import os
import re
import logging
import unicodedata
import requests
import time
import ast
import tiktoken
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask import Flask, request
from openai import OpenAI
from supabase import create_client, Client
from twilio.rest import Client as TwilioClient
import numpy as np
from bot_config import BOT_PLANTILLAS
from bot_config import obtener_plantilla, validar_plantillas
from intencion_embeddings import analizar_intencion_con_embeddings
from kpi import registrar_kpi_evento
from aprendizaje import guardar_frase_conversion
import threading
from collections import defaultdict
import json
import difflib
from dateutil import parser 
from zoneinfo import ZoneInfo
from citas_bot import gestionar_reserva, actualizar_estado, manejar_confirmacion_o_zona, iniciar_citas_bot
from handlers.perfil_comportamiento import actualizar_perfil_comportamiento_usuario
from flask_cors import CORS
from flask import Flask, request

# === Helpers de envío centralizados ===

def send_and_log(user_id, texto, conversation_id=None, tipo="text"):
    # 1) Envío a WhatsApp
    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        to=f"whatsapp:{user_id}",
        body=texto
    )
    # 2) Log en Supabase
    supabase.from_("interaction_history").insert({
        "conversation_id": conversation_id,
        "sender_role": "bot",
        "message_type": tipo,
        "bot_response": texto,
        "start_time": datetime.now(timezone.utc).isoformat()
    }).execute()

MEXICO_TZ = ZoneInfo("America/Mexico_City")
ahora_mx = datetime.now(MEXICO_TZ)
fecha_actual_str = ahora_mx.strftime("%A %d de %B de %Y")
hora_actual_str = ahora_mx.strftime("%H:%M")


# Cargar variables de entorno
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
#TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
FB_VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN")
twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH)

if os.getenv("ENV") == "development":
    print("🧪 Validando integridad de plantillas...")
    validar_plantillas()


# Inicializar servicios
client = OpenAI(api_key=OPENAI_API_KEY)
#telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Estados
conversations = {}
estado_usuario = {}
ultimo_mensaje = {}

# Buffer para juntar mensajes si el usuario escribe en varias partes
mensaje_buffer = defaultdict(list)
hora_ultimo_buffer = {}

MINUTOS_PARA_CERRAR = 3



def guardar_conversacion_si_no_existe(user_id, numero, tipo_negocio="generico"):
    numero_limpio = numero.replace("+", "")
    try:
        existing = supabase.from_("conversation_history")\
            .select("conversation_id")\
            .eq("whatsapp_number", numero_limpio)\
            .limit(1)\
            .execute()
        
        if existing.data and len(existing.data) > 0:
            return existing.data[0]["conversation_id"]
        else:
            nueva = supabase.from_("conversation_history").insert({
                "whatsapp_number": numero_limpio,
                "channel": "whatsapp",
                "user_identifier": user_id,
                "user_id": user_id,
                "status": "bot",
                "is_human": False,
                "tipo_negocio": tipo_negocio  # ✅ Aquí ya no marcará error
            }).execute()
            logging.info(f"🆕 Conversación creada: {nueva}")
            return nueva.data[0]["conversation_id"] if nueva.data else None
    except Exception as e:
        logging.error(f"❌ Error accediendo a Supabase: {e}")
        return None


def guardar_mensaje_conversacion(conversation_id, mensaje, sender="user", tipo="text", file_url=None):
    if not conversation_id:
        logging.warning("⚠️ No se proporcionó conversation_id. No se guardará el mensaje.")
        return None

    field_name = "user_message" if sender == "user" else "bot_response"
    insert_data = {
        "conversation_id": conversation_id,
        "sender_role": sender,
        "message_type": tipo,
        "start_time": datetime.now(timezone.utc).isoformat(),
        field_name: mensaje
    }

    # ✅ Si hay una URL, agrégala al insert
    if file_url:
        insert_data["file_url"] = file_url

    try:
        nuevo = supabase.from_("interaction_history").insert(insert_data).execute()
        message_id = nuevo.data[0]["id"] if nuevo.data else None
        logging.info(f"💬 Mensaje guardado con ID: {message_id}")
        return message_id
    except Exception as e:
        logging.error(f"❌ Error guardando mensaje en interaction_history: {e}")
        return None


def buscar_fragmento_relevante(texto, top_k=3):
    try:
        # 1. Obtener embedding del texto
        embedding_res = client.embeddings.create(
            model="text-embedding-ada-002",
            input=texto
        )
        embedding_vector = embedding_res.data[0].embedding

        # 2. Ejecutar la función RPC simple
        args_rpc = {
            "query_embedding": embedding_vector,
            "match_count": top_k
        }

        logging.info("🔎 Buscando fragmentos relevantes en user_files...")

        response = supabase.rpc("match_user_files", args_rpc).execute()

        # 3. Validar resultado
        if not response.data:
            logging.warning("⚠️ No se encontraron fragmentos similares.")
            return []

        fragmentos = response.data

        for i, frag in enumerate(fragmentos):
            logging.info(f"📂 Fragmento #{i+1}: {frag.get('analysis_embedding_text', '')[:100]}...")

        return fragmentos

    except Exception as e:
        logging.error(f"❌ Error en búsqueda de fragmentos: {e}")
        return []


def detectar_cierre_conversacion(user_id, msg, ultimo_mensaje, tiempo_inactividad_min=10):
    now = datetime.now()
    texto = msg.strip().lower()
    FRASES_CIERRE = [
        "eso es todo", "sería todo", "me despido", "nos vemos", "adiós",
        "puedes cerrar la conversación", "no tengo más dudas", "por el momento es todo",
        "hasta luego", "gracias por tu ayuda", "gracias por la información",
        "puedes finalizar", "puedes cerrar", "listo", "ya está", "ya quedó", "ok gracias"
    ]
    # Si detecta frase de cierre clara, cierra ya
    if any(f in texto for f in FRASES_CIERRE) and "?" not in texto:
        return "cerrar_ya"
    # Si hay inactividad
    if user_id in ultimo_mensaje:
        inactivo_por = now - ultimo_mensaje[user_id]
        if inactivo_por > timedelta(minutes=tiempo_inactividad_min):
            return "cerrar_ya"
    return "activa"


def generar_analisis_mensaje_unico(mensaje: str):
    try:
        prompt = """
Eres un analista de comportamiento para ventas.

Analiza el siguiente mensaje único del usuario y responde con:

Intención: [interés | duda | objeción | rechazo | sin intención]  
Tono: [positivo | negativo | neutro]  
Relevancia: [alta | media | baja]  
Observación: [1 línea con observación útil para ventas]

Ejemplo de salida:  
Intención: interés  
Tono: positivo  
Relevancia: alta  
Observación: Muestra interés en cotizar un producto.
"""
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": mensaje}
            ],
            max_tokens=100,
            temperature=0.6
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"❌ Error en análisis 1:1: {e}")
        return None


def generar_analisis_incremental(conversacion):
    try:
        prompt = """
Eres un analista de comportamiento de ventas.

Analiza este bloque de conversación (usuario y bot) y resume en 1 línea:

- La intención actual del usuario.
- Cómo ha cambiado el tono.
- Qué oportunidad comercial se detecta.

Formato:  
Intención: [interés | duda | objeción | rechazo | sin intención]  
Tono: [positivo | negativo | neutro]  
Oportunidad: [frase breve con insight comercial]

Ejemplo:  
Intención: duda  
Tono: neutro  
Oportunidad: Pidió información sobre garantías del servicio.
"""
        full = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in conversacion])
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": full}
            ],
            max_tokens=120,
            temperature=0.6
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"❌ Error en análisis incremental: {e}")
        return None


def generar_analisis_final_openai(conversacion):
    try:
        full = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in conversacion])
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": """
Eres un analista experto en marketing conversacional.

Con base en toda esta conversación entre un usuario y un asistente virtual, responde de forma estructurada y clara los siguientes campos:

Resumen Final:
Intereses: [resumen claro]
Dudas frecuentes: [resumen claro]
Intención de compra: [alta | media | baja | nula]
Observaciones para ventas: [máx 1 línea útil para seguimiento]
Tono: [positivo | negativo | neutro]
"""},
                {"role": "user", "content": full}
            ],
            max_tokens=500,
            temperature=0.6
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Error generando análisis final: {e}")
        return None

    
def clasificar_lead(conversacion, conversation_id):
    try:
        # Obtener configuración del bot (incluye el contexto correcto)
        config = obtener_configuracion_bot(conversation_id)

        texto = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in conversacion])

        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": config["contexto"] + """

Ahora, clasifica este usuario como: calificado, medio o no calificado.

- calificado: Si cumple con los requisitos clave según tu lógica de negocio (ej. edad, interés, documentos).
- medio: Si muestra interés pero falta al menos un requisito.
- no calificado: Si no cumple con los mínimos, no responde o no es relevante.

Responde solo con una palabra: calificado, medio o no calificado.
"""},  # Puedes mejorar esta lógica por bot_type si deseas
                {"role": "user", "content": texto}
            ],
            max_tokens=10,
            temperature=0
        )

        return response.choices[0].message.content.strip().lower().replace(" ", "_")

    except Exception as e:
        logging.error(f"Error clasificando lead: {e}")
        return "no calificado"


def actualizar_etapa_conversacion(conversation_id, etapa):
    try:
        supabase.from_("conversation_history").update({
            "etapa_actual": etapa,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("conversation_id", conversation_id).execute()
        logging.info(f"🔖 Etapa actualizada a '{etapa}' para conversación {conversation_id}")
    except Exception as e:
        logging.error(f"❌ Error actualizando etapa_actual: {e}")


def actualizar_lead_score(conversation_id, score):
    try:
        supabase.from_("conversation_history").update({
            "lead_score": score,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("conversation_id", conversation_id).execute()
        logging.info(f"🎯 Lead_score actualizado a '{score}'")
    except Exception as e:
        logging.error(f"❌ Error actualizando lead_score: {e}")


def reevaluar_lead_score_dinamico(conversation_id, conversacion, user_id):
    try:
        resultado = supabase.from_("conversation_history")\
            .select("lead_score")\
            .eq("conversation_id", conversation_id)\
            .limit(1)\
            .execute()

        score_actual = resultado.data[0]["lead_score"] if resultado.data else None
        nuevo_score = clasificar_lead(conversacion, conversation_id).strip().lower().replace(" ", "_")

        if nuevo_score and nuevo_score != (score_actual or "").strip().lower().replace(" ", "_"):
            actualizar_lead_score(conversation_id, nuevo_score)
            logging.info(f"🔄 Lead_score actualizado: {score_actual} → {nuevo_score}")

            # Evitar sobrescribir etapas clave como 'cotizacion_enviada' o 'seguimiento'
            etapa_actual = supabase.from_("conversation_history")\
                .select("etapa_actual")\
                .eq("conversation_id", conversation_id)\
               .limit(1).execute()
            etapa = etapa_actual.data[0]["etapa_actual"] if etapa_actual.data else None

            # Solo cambiar etapa si está en una etapa genérica o inicial
            etapas_bloqueadas = ["cotizacion_enviada", "seguimiento"]
            if etapa not in etapas_bloqueadas:
                if nuevo_score in ["calificado", "medio"]:
                    actualizar_etapa_conversacion(conversation_id, "seguimiento")
                elif nuevo_score == "no_calificado":
                    actualizar_etapa_conversacion(conversation_id, "no_calificado")

            # Guardar historial
            try:
                supabase.from_("lead_score_history").insert({
                    "conversation_id": conversation_id,
                    "old_score": score_actual,
                    "new_score": nuevo_score,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }).execute()
                logging.info("📈 Historial de lead_score guardado correctamente")
            except Exception as e:
                logging.warning(f"⚠️ No se pudo guardar historial de score: {e}")

            # Verifica que exista en conversation_history (fallback defensivo)
            try:
                existente = supabase.from_("conversation_history")\
                    .select("conversation_id")\
                    .eq("conversation_id", conversation_id)\
                    .limit(1)\
                    .execute()

                if not existente.data:
                    supabase.from_("conversation_history").insert({
                        "conversation_id": conversation_id,
                        "lead_score": nuevo_score,
                        "channel": "whatsapp",
                        "user_id": user_id,
                        "user_identifier": user_id,
                        "status": "bot",
                        "is_human": False,
                        "created_at": datetime.now(timezone.utc).isoformat()
                    }).execute()
                    logging.info("📝 Lead registrado también en conversation_history")
            except Exception as e:
                logging.warning(f"⚠️ No se pudo insertar en conversation_history: {e}")

    except Exception as e:
        logging.error(f"❌ Error reevaluando lead_score dinámicamente: {e}")



def send_messenger_message(sender_id, message):
    PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
    url = f"https://graph.facebook.com/v17.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": sender_id},
        "message": {"text": message}
    }
    headers = {"Content-Type": "application/json"}
    try:
        r = requests.post(url, json=payload, headers=headers)
        r.raise_for_status()
        logging.info(f"✅ Respuesta enviada a Messenger: {message}")
    except Exception as e:
        logging.error(f"❌ Error al enviar mensaje a Messenger: {e}")


def guardar_conversacion_messenger_si_no_existe(user_id, sender_id):
    try:
        existing = supabase.from_("conversation_history")\
            .select("conversation_id")\
            .eq("messenger_id", sender_id)\
            .limit(1)\
            .execute()

        if existing.data and len(existing.data) > 0:
            return existing.data[0]["conversation_id"]
        else:
            nueva = supabase.from_("conversation_history").insert({
                "messenger_id": sender_id,
                "channel": "messenger",
                "user_identifier": user_id,
                "user_id": user_id,
                "status": "bot",
                "is_human": False,
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()
            logging.info(f"🆕 Conversación Messenger creada: {nueva}")
            return nueva.data[0]["conversation_id"] if nueva.data else None
    except Exception as e:
        logging.error(f"❌ Error accediendo a Supabase (Messenger): {e}")
        return None



def construir_fragmento_resumido_por_tokens(fragmentos, max_tokens=7000, modelo="gpt-4"):
    encoding = tiktoken.encoding_for_model(modelo)
    bloques = []
    bloque_actual = ""
    tokens_actual = 0
    limite_seguro = max_tokens

    for frag in fragmentos:
        contenido = frag.get("analysis_embedding_text", "").strip()
        if not contenido:
            continue

        fragmento_texto = f"[Fragmento del proyecto]\n{contenido.strip()}\n"
        tokens_fragmento = len(encoding.encode(fragmento_texto))

        if tokens_actual + tokens_fragmento > limite_seguro:
            if bloque_actual:
                bloques.append(bloque_actual.strip())
            # Reiniciar nuevo bloque
            bloque_actual = fragmento_texto
            tokens_actual = tokens_fragmento
        else:
            bloque_actual += fragmento_texto
            tokens_actual += tokens_fragmento

    if bloque_actual:
        bloques.append(bloque_actual.strip())

    return bloques


def dividir_mensaje_largo(texto, max_chars=1500):
    partes = []
    while len(texto) > max_chars:
        corte = texto.rfind("\n", 0, max_chars)
        corte = corte if corte != -1 else max_chars
        partes.append(texto[:corte].strip())
        texto = texto[corte:].strip()
    partes.append(texto)
    return partes


def obtener_configuracion_bot(_conversation_id=None):
    try:
        config = supabase.from_("bot_configuracion")\
            .select("bot_type, tipo_negocio, ubicacion, latitud, longitud, contexto, tipo_reservas, negocio, requiere_zona, nombre_bot, zonas_permitidas")\
            .order("created_at", desc=False)\
            .limit(1)\
            .execute()

        if not config.data:
            raise Exception("❌ No se encontró ninguna configuración del bot")

        datos = config.data[0]

        # Normalizar bot_type a lista
        bot_types = datos.get("bot_type", [])
        if not isinstance(bot_types, list):
            bot_types = [bot_types] if bot_types else ["ventas"]

        tipo_negocio = datos.get("tipo_negocio", "generico")  # ✅ Extraer tipo de negocio

        # ✅ Combinar funciones de todas las plantillas activas
        funciones_combinadas = set()
        usa_embeddings = False

        for tipo in bot_types:
            plantilla = BOT_PLANTILLAS.get(tipo, {})
            funciones_combinadas.update(plantilla.get("funciones", []))
            if plantilla.get("usa_embeddings"):
                usa_embeddings = True

        plantilla_base = {
            "descripcion": "Bot combinado",
            "funciones": list(funciones_combinadas)
        }

        return {
            "bot_type": bot_types,
            "tipo_negocio": tipo_negocio,  # ✅ Incluir en retorno
            "ubicacion": datos.get("ubicacion", ""),
            "latitud": datos.get("latitud"),
            "longitud": datos.get("longitud"),
            "contexto": datos.get("contexto", ""),
            "tipo_reservas": datos.get("tipo_reservas", "único_horario"),
            "negocio": datos.get("negocio", "generico"),
            "requiere_zona": datos.get("requiere_zona", False),
            "zonas_permitidas": datos.get("zonas_permitidas", ["Salón", "Terraza", "VIP"]),
            "nombre_bot": datos.get("nombre_bot", "AIDANA"),
            "prompt": generar_prompt(
                plantilla_base,
                datos.get("contexto", ""),
                datos.get("nombre_bot", "Asistente"),
                tipo_negocio  # ✅ Pasar tipo_negocio al prompt
            ),
            "usa_embeddings": usa_embeddings
        }

    except Exception as e:
        logging.error(f"❌ Fallback a ventas. Error: {e}")
        plantilla = BOT_PLANTILLAS["ventas"]
        return {
            "bot_type": ["ventas"],
            "tipo_negocio": "generico",
            "ubicacion": "",
            "latitud": None,
            "longitud": None,
            "contexto": "",
            "tipo_reservas": "único_horario",
            "negocio": "generico",
            "requiere_zona": False,
            "zonas_permitidas": ["Salón", "Terraza", "VIP"],
            "nombre_bot": "Asistente",
            "prompt": generar_prompt(plantilla, "", "Asistente", "generico"),
            "usa_embeddings": plantilla.get("usa_embeddings", False)
        }




def generar_prompt(plantilla, contexto_extra="", nombre_bot="Asistente", tipo_negocio="generico"):
    funciones = set(plantilla.get("funciones", []))

    instrucciones = [
        f"{plantilla['descripcion']} Hola, soy AIDANA, tu asistente virtual de {nombre_bot}."
        f"Este asistente está diseñado para un negocio tipo '{tipo_negocio}'.",
        "Actúa siempre con claridad y amabilidad. Tu propósito es asistir al usuario según sus necesidades."
    ]

    if "agendar_reserva" in funciones:
        instrucciones.append("- Si el usuario quiere agendar una cita, demo o reserva, pide fecha, hora y zona (si aplica).")
    if "enviar_recordatorios" in funciones:
        instrucciones.append("- Envía recordatorios antes de citas si es parte del flujo.")
    if "cancelar_reserva" in funciones:
        instrucciones.append("- Si el usuario desea cancelar, confirma y registra la cancelación.")
    if "post_servicio" in funciones:
        instrucciones.append("- Después del servicio, puedes solicitar calificación o hacer seguimiento.")
    if "calificar_leads" in funciones:
        instrucciones.append("- Si el usuario muestra interés, califícalo como lead y propón el siguiente paso.")
        instrucciones.append("- Si el usuario muestra interés pero no decide, sugiere de forma amable un siguiente paso como agendar, cotizar o dejar sus datos.")
        instrucciones.append("- Si pregunta por precios o productos, ofrece ayudar con una propuesta o preguntar si desea más detalles.")
        instrucciones.append("- Cierra tus respuestas con una acción sugerida siempre que sea posible: '¿Quieres que te envíe una propuesta?', '¿Deseas agendar tu cita ahora?', etc.")
    if "responder_por_embeddings" in funciones:
        instrucciones.append("- Si el usuario hace preguntas específicas, usa los documentos entrenados para responder.")
    if "analizar_archivos" in funciones:
        instrucciones.append("- Si el usuario sube archivos, puedes analizarlos y dar retroalimentación clara.")
    if "generar_faq" in funciones:
        instrucciones.append("- Si detectas una duda frecuente, puedes sugerir una respuesta tipo FAQ.")
    if "gestionar_tareas" in funciones:
        instrucciones.append("- Puedes organizar tareas si el usuario lo solicita.")
    if "crear_recordatorio" in funciones:
        instrucciones.append("- Puedes programar recordatorios según lo que el usuario diga.")
    if "dar_seguimiento" in funciones:
        instrucciones.append("- Puedes dar seguimiento si el usuario no responde o deja algo pendiente.")
        instrucciones.append("- Si detectas que el usuario quedó en pensar, preguntar o revisar algo, puedes recordarle amablemente después de unos minutos.")
        instrucciones.append("- Si el usuario pidió info y no ha respondido en un rato, puedes enviar un mensaje como: '¿Tuviste oportunidad de revisarlo? Estoy aquí para ayudarte.'")

    # Instrucciones generales
    instrucciones.append("- No digas frases genéricas como 'estoy aquí para ayudarte'. Sé directo pero cordial.")
    instrucciones.append("- Si no tienes suficiente contexto, puedes pedirlo de forma amable.")

    prompt_final = "\n".join(instrucciones).strip()

    if contexto_extra:
        prompt_final += f"\n\n{contexto_extra.strip()}"

    return prompt_final



def generar_seguimiento_contextual(conversation_id):
    try:
        historial_res = supabase.from_("interaction_history")\
            .select("sender_role, user_message, bot_response")\
            .eq("conversation_id", conversation_id)\
            .order("start_time", desc=True)\
            .limit(6)\
            .execute()

        historial_raw = historial_res.data or []

        historial = []
        for item in reversed(historial_raw):
            if item.get("sender_role") == "user" and item.get("user_message"):
                historial.append({"role": "user", "content": item["user_message"]})
            elif item.get("sender_role") == "bot" and item.get("bot_response"):
                historial.append({"role": "assistant", "content": item["bot_response"]})

        if not historial:
            return None

        prompt = """
Eres un asistente virtual que busca dar seguimiento humano, cálido y natural a un usuario que no ha respondido en un tiempo.

Con base en el historial de los últimos mensajes, genera un mensaje breve, sutil y profesional para retomar la conversación de forma amable. Evita sonar repetitivo o robótico. Sé claro, directo y personalizado.

Ejemplo (si el usuario pidió una cotización): 
"Hola, buen día. Solo quería saber si pudiste revisar la propuesta que te compartimos. Estoy aquí para ayudarte con cualquier duda."

No repitas el historial. Solo responde con el mensaje de seguimiento.
"""

        respuesta = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "\n".join([f"{m['role']}: {m['content']}" for m in historial])}
            ],
            temperature=0.6,
            max_tokens=80
        )
        return respuesta.choices[0].message.content.strip()

    except Exception as e:
        logging.error(f"❌ Error generando seguimiento contextual: {e}")
        return None

def seguimiento_leads_silenciosos():
    print("⏳ Loop de seguimiento activo")

    mensajes_por_negocio = {
        "restaurante": "🍽️ ¿Te gustaría confirmar tu reserva o consultar disponibilidad?",
        "clinica": "👩‍⚕️ ¿Tuviste oportunidad de revisar sobre la consulta? Estoy aquí si quieres agendar.",
        "agencia": "🚗 ¿Te interesa agendar una cita o cotizar un auto? Estoy para ayudarte.",
        "software": "💻 ¿Pudiste revisar la propuesta? Estoy aquí si tienes dudas o quieres avanzar.",
        "marketing": "📢 ¿Te gustaría que revisemos juntos una estrategia? Podemos agendar.",
        "spa": "💆‍♀️ ¿Quieres confirmar tu cita o saber más de nuestros servicios?",
        "educacion": "📚 ¿Te interesa apartar lugar o ver próximos cursos?",
        "eventos": "💍 ¿Te gustaría cotizar o reservar tu evento con nosotros?",
        "ecommerce": "🛒 ¿Pudiste ver los productos? Si necesitas ayuda con tu pedido, dime.",
        "hotel": "🏨 ¿Aún estás interesado en reservar alojamiento? Podemos ayudarte.",
        "legal": "⚖️ ¿Puedo asistirte con tu consulta legal o agendarte con un abogado?",
        "generico": "👋 Solo quería saber si pudiste revisar lo que te compartí. ¿Te gustaría que te ayude con algo más?"
    }

    tiempos_por_lead_score = {
        "calificado": timedelta(hours=72),
        "medio": timedelta(hours=48),
        "no_calificado": timedelta(hours=24)
    }

    max_intentos = {
        "calificado": 3,
        "medio": 3,
        "no_calificado": 3
    }

    while True:
        try:
            ahora = datetime.now(timezone.utc)

            resultado = supabase.from_("conversation_history")\
                .select("conversation_id, user_id, updated_at, status, etapa_actual, tipo_negocio, lead_score, seguimiento_enviado_at, intentos_seguimiento, cotizacion_enviada_at, cotizacion_enviada")\
                .eq("status", "bot")\
                .execute()

            for conv in resultado.data or []:
                conversation_id = conv["conversation_id"]
                user_id = conv["user_id"]
                tipo = conv.get("tipo_negocio", "generico")
                etapa = conv.get("etapa_actual", "nuevo")
                lead_score = conv.get("lead_score", "no_calificado")
                seguimiento_at = conv.get("seguimiento_enviado_at")
                intentos = conv.get("intentos_seguimiento", 0) or 0
                ultima_interaccion = parser.isoparse(conv["updated_at"])
                cotizacion_enviada = conv.get("cotizacion_enviada", False)  # <-- SUMADO

                # ⛔️ Si ya superó los intentos para su tipo
                if intentos >= max_intentos.get(lead_score, 3):
                    continue

                # ⏰ Tiempo mínimo de espera por lead
                tiempo_minimo = tiempos_por_lead_score.get(lead_score, timedelta(hours=24))
                if etapa == "cotizacion_enviada":
                    # SOLO hacer seguimiento si el booleano está en True
                    if not cotizacion_enviada:
                        continue  # 🔴 Salta si la cotización NO fue marcada como enviada
                    tiempo_minimo = timedelta(hours=72)  # 3 días

                # ⏱️ Verificar si ya pasó suficiente tiempo
                ultima_base = parser.isoparse(seguimiento_at) if seguimiento_at else ultima_interaccion
                if ahora - ultima_base < tiempo_minimo:
                    continue

                # 🛑 Usuario ya respondió después de la cotización
                cotizacion_enviada_at = conv.get("cotizacion_enviada_at")
                if cotizacion_enviada_at:
                    cotizacion_time = parser.isoparse(cotizacion_enviada_at)
                    if ultima_interaccion > cotizacion_time:
                        logging.info(f"🛑 Usuario {user_id} ya respondió después de la cotización. No se enviará seguimiento.")
                        continue

                # 🧠 Intentar generar seguimiento contextual
                mensaje = generar_seguimiento_contextual(conversation_id)
                if not mensaje:
                    mensaje = mensajes_por_negocio.get(tipo, mensajes_por_negocio["generico"])
                    logging.warning(f"⚠️ Usando mensaje genérico para seguimiento a {user_id}")

                 # 📤 Enviar mensaje y loguear
                send_and_log(user_id, mensaje, conversation_id, tipo="text")
                logging.info(f"📬 Seguimiento enviado a {user_id} (intento #{intentos + 1})")

                # 📝 Registrar seguimiento
                supabase.from_("conversation_history").update({
                    "seguimiento_enviado_at": ahora.isoformat(),
                    "intentos_seguimiento": intentos + 1
                }).eq("conversation_id", conversation_id).execute()

        except Exception as e:
            logging.error(f"❌ Error en seguimiento a leads silenciosos: {e}")

        time.sleep(1800)  # ⏳ Esperar 30 minutos



def es_intencion_de_reservar(texto):
    texto = texto.lower()
    palabras_clave = [
        "reservar", "reserva", "agendar", "quiero una cita",
        "quiero hacer una cita", "quiero hacer una reserva",
        "agenda", "necesito una cita", "quiero agendar",
        "me puedes apartar", "quiero una mesa", "hacer cita"
    ]
    return any(palabra in texto for palabra in palabras_clave)

def es_intencion_reserva_por_embeddings(texto, threshold=0.80):
    try:
        embedding_usuario = client.embeddings.create(
            model="text-embedding-ada-002",
            input=texto
        ).data[0].embedding

        ejemplos_intencion = [
            "quiero reservar una cita",
            "cómo agendo una consulta",
            "necesito un turno para mañana",
            "quisiera apartar una hora",
            "cómo hago una reserva"
        ]

        ejemplos_embeddings = client.embeddings.create(
            model="text-embedding-ada-002",
            input=ejemplos_intencion
        ).data

        for ej in ejemplos_embeddings:
            emb_ejemplo = ej.embedding
            similitud = cosine_similarity(embedding_usuario, emb_ejemplo)
            if similitud >= threshold:
                return True
        return False

    except Exception as e:
        logging.error(f"❌ Error usando embeddings para intención de reserva: {e}")
        return False

def cosine_similarity(vec1, vec2):
    v1 = np.array(vec1)
    v2 = np.array(vec2)
    return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

def detectar_intencion_reserva(texto):
    if es_intencion_de_reservar(texto):
        return True

    # Solo si falla el filtro rápido, prueba con embeddings
    return es_intencion_reserva_por_embeddings(texto)


def detectar_intencion_comercial_o_reserva(msg, contexto_negocio=""):
    try:
        prompt_sistema = f"""
Eres un analista de intenciones para un asistente virtual.

Clasifica el siguiente mensaje como una de las siguientes opciones:
- venta → si el usuario muestra interés comercial, pide información, precios o contacto.
- reserva → si quiere agendar una cita o hacer una reservación.
- ambos → si hace ambas cosas.
- ninguno → si el mensaje no tiene intención clara.

Ten en cuenta el siguiente contexto del negocio:
"{contexto_negocio}"

Responde únicamente con: venta, reserva, ambos o ninguno.
"""
        respuesta = client.chat.completions.create(
            model="gpt-3.5-turbo",
            temperature=0.2,
            messages=[
                {"role": "system", "content": prompt_sistema.strip()},
                {"role": "user", "content": msg}
            ],
            max_tokens=5
        )

        return respuesta.choices[0].message.content.strip().lower()

    except Exception as e:
        logging.error(f"❌ Error detectando intención: {e}")
        return "ninguno"


def obtener_funciones_disponibles(bot_type):
    funciones = set()
    if isinstance(bot_type, list):
        for tipo in bot_type:
            funciones.update(BOT_PLANTILLAS.get(tipo, {}).get("funciones", []))
    else:
        funciones.update(BOT_PLANTILLAS.get(bot_type, {}).get("funciones", []))
    return funciones


def obtener_historial_contexto(user_id, max_mensajes=40):
    return conversations.get(user_id, [])[-max_mensajes:]

def es_intencion_reagendar(texto):
    texto = texto.lower()
    frases = [
        "cambiar mi cita", "modificar mi cita", "reagendar", "reprogramar", 
        "mover la cita", "cambiar la hora", "otra hora", "otra fecha", 
        "cambiar fecha", "nueva fecha", "nueva hora", "ajustar cita", "editar cita"
    ]
    return any(frase in texto for frase in frases)


def obtener_reserva_activa(user_id):
    try:
        resultado = supabase.from_("reservaciones")\
            .select("id")\
            .eq("user_id", user_id)\
            .in_("estado", ["pendiente", "confirmada"])\
            .order("fecha_reserva", desc=True)\
            .limit(1)\
            .execute()
        return resultado.data[0] if resultado.data else None
    except Exception as e:
        logging.error(f"❌ Error buscando reserva activa: {e}")
        return None

def actualizar_reserva(reserva_id, nuevos_datos):
    try:
        resultado = supabase.from_("reservaciones").update(nuevos_datos)\
            .eq("id", reserva_id).execute()
        logging.info(f"🔁 Reserva ACTUALIZADA (ID: {reserva_id}) con datos: {nuevos_datos}")
        return True
    except Exception as e:
        logging.error(f"❌ Error actualizando reserva (ID: {reserva_id}): {e}")
        return False





def procesar_buffer_usuario(user_id, nuevo_mensaje, ventana=10, max_mensajes=3):
    ahora = datetime.now()
    ultimo = hora_ultimo_buffer.get(user_id)

    # Si pasó mucho tiempo desde el último → reiniciamos buffer
    if not ultimo or (ahora - ultimo).seconds > ventana:
        mensaje_buffer[user_id] = [nuevo_mensaje]
    else:
        mensaje_buffer[user_id].append(nuevo_mensaje)

    hora_ultimo_buffer[user_id] = ahora

    # Si ya se juntaron suficientes mensajes, fusionamos y reiniciamos
    if len(mensaje_buffer[user_id]) >= max_mensajes:
        mensaje_completo = " ".join(mensaje_buffer[user_id]).strip()
        mensaje_buffer[user_id] = []
        return mensaje_completo

    # Si no hemos llegado al límite, solo mostramos el acumulado (pero no reiniciamos)
    return " ".join(mensaje_buffer[user_id]).strip()



def guardar_resumen_usuario(user_id, _conversation_id=None):
    try:
        logging.info(f"🔍 Generando resumen para user_id: {user_id}")

        # 1. Buscar todas las conversaciones del usuario
        conversaciones_res = supabase.from_("conversation_history")\
            .select("conversation_id")\
            .ilike("user_id", f"%{user_id}")\
            .execute()

        conversaciones = conversaciones_res.data or []

        if not conversaciones:
            logging.warning(f"⚠️ No hay conversaciones para el usuario {user_id}")
            return

        conversation_ids = [c["conversation_id"] for c in conversaciones]

        # 2. Obtener interacciones de TODAS sus conversaciones
        interacciones_res = supabase.from_("interaction_history")\
            .select("sender_role, user_message, bot_response, start_time")\
            .in_("conversation_id", conversation_ids)\
            .order("start_time", desc=False)\
            .limit(200)\
            .execute()

        interacciones = interacciones_res.data or []

        if not interacciones:
            logging.warning("⚠️ No hay interacciones suficientes para resumir.")
            return

        # 3. Armar historial para GPT
        historial = []
        for i in interacciones:
            if i.get("sender_role") == "user" and i.get("user_message"):
                historial.append(f"Usuario: {i['user_message']}")
            elif i.get("sender_role") == "bot" and i.get("bot_response"):
                historial.append(f"Bot: {i['bot_response']}")

        texto_conversacion = "\n".join(historial[-50:])  # Últimos 30 intercambios

        # 4. Generar resumen con GPT
        prompt = """
Resume de forma clara la intención, temas tratados, objeciones y estilo del usuario.
El objetivo es guardar una memoria útil para futuras conversaciones.
Ejemplo:
Interés en SUV, menciona presupuesto ajustado, desea agendar prueba. Usa lenguaje informal pero decidido.
"""
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": texto_conversacion}
            ],
            temperature=0.5,
            max_tokens=150
        )
        resumen = response.choices[0].message.content.strip()
        logging.info(f"📝 Resumen generado: {resumen}")

        # 5. Generar embedding
        embedding_response = client.embeddings.create(
            model="text-embedding-ada-002",
            input=resumen
        )
        vector = embedding_response.data[0].embedding

        # 6. Guardar en Supabase
        supabase.from_("user_summary_embeddings").insert({
            "user_id": user_id,
            "resumen_texto": resumen,
            "embedding_vector": vector
        }).execute()

        logging.info(f"🧠 Resumen global de usuario guardado para {user_id}")

    except Exception as e:
        logging.error(f"❌ Error generando memoria del usuario: {e}")



def obtener_memoria_usuario(user_id, cuantos=3):
    try:
        resultado = supabase.from_("user_summary_embeddings")\
            .select("resumen_texto, created_at")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(cuantos)\
            .execute()
        # Junta los resúmenes de más nuevo a más viejo
        if resultado.data:
            return "\n".join([r["resumen_texto"] for r in reversed(resultado.data)])
        return None
    except Exception as e:
        logging.error(f"❌ Error obteniendo memoria del usuario: {e}")
        return None



def detectar_intencion_directa(texto, tipo):
    texto_normalizado = ''.join(
        c for c in unicodedata.normalize('NFD', texto.lower())
        if unicodedata.category(c) != 'Mn'
    )

    patrones = {
        "ubicacion": [
            "ubicacion", "direccion", "donde estan", "donde se ubican",
            "estan ubicados", "me puede mandar la ubicacion", "donde se encuentran"
        ],
        "horarios": [
            "horario", "a que hora", "cuando abren", "cuando cierran",
            "cual es su horario", "estan abiertos"
        ],
        "precios": [
            "precio", "cuanto cuesta", "tienen precios", "tarifas", "costo", "vale"
        ]
    }

    return any(p in texto_normalizado for p in patrones.get(tipo, []))

def enviar_ubicacion(usuario_id: str, ubicacion: str, lat: float = None, lng: float = None, conversation_id: str = None, config: dict = {}):
    try:
        if lat and lng:
            mensaje_texto = f"📍 Nuestra dirección es:\n{ubicacion}\n\nEnseguida te comparto la ubicación en el mapa."
            # 1) envío + log
            send_and_log(usuario_id, mensaje_texto, conversation_id, tipo="text")

            mensaje_mapa = "🗺️ Da clic aquí para abrir el mapa:"
            # 2) Este queda directo porque necesita la acción geo
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=f"whatsapp:{usuario_id}",
                persistent_action=[f"geo:{lat},{lng}|{ubicacion}"],
                body=mensaje_mapa
            )
            # y lo logueas manualmente como tipo "location":
            supabase.from_("interaction_history").insert({
                "conversation_id": conversation_id,
                "sender_role": "bot",
                "message_type": "location",
                "bot_response": mensaje_mapa,
                "file_url": f"https://www.google.com/maps?q={lat},{lng}",
                "start_time": datetime.now(timezone.utc).isoformat()
            }).execute()

        else:
            mensaje_texto = f"📍 Nuestra dirección es:\n{ubicacion}"
            send_and_log(usuario_id, mensaje_texto, conversation_id, tipo="text")

        logging.info(f"✅ Ubicación enviada correctamente a {usuario_id}")
    except Exception as e:
        logging.error(f"❌ Error al enviar ubicación: {e}")



def generar_respuesta_promocion(texto_extraido: str) -> str:
    hoy = datetime.now().strftime("%d de %B de %Y")
    prompt = f"""
Eres un asistente de atención al cliente amable y profesional. Tu tarea es analizar el siguiente texto de una imagen o PDF que podría contener una promoción.

Tu objetivo es:
1. Detectar si el texto contiene una promoción.
2. Si contiene promoción, indicar si está vigente al día de hoy ({hoy}).
3. Generar una respuesta NATURAL y amable para el cliente con base en lo anterior.

Instrucciones:
- Si está vigente, responde algo como:
  "🎉 ¡Claro! Tenemos una promoción activa que sigue vigente: [resumen breve]."

- Si ya venció, responde algo como:
  "📌 Esa promoción ya finalizó, pero puedo ayudarte con otras opciones actuales."

- Si no es una promoción, responde algo como:
  "🧐 No encontré información de promociones en ese archivo. ¿Quieres que revise otra cosa?"

Texto para analizar:
\"\"\"{texto_extraido}\"\"\"
"""
    respuesta = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": prompt}
        ],
        max_tokens=200,
        temperature=0.5
    )
    return respuesta.choices[0].message.content.strip()


def obtener_ultimos_archivos(user_id, limite=3):
    try:
        resultado = supabase.from_("user_files")\
            .select("file_name, file_url, embedding_vector")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(limite)\
            .execute()
        return resultado.data or []
    except Exception as e:
        logging.error(f"❌ Error al obtener archivos recientes: {e}")
        return []

def responder_y_salir(user_id: str, mensaje: str, conversation_id: str = None):
    # 1) Envío + log en un único sitio
    send_and_log(user_id, mensaje, conversation_id, tipo="text")
    logging.info(f"📤 Respuesta final enviada. Conversación terminada.")
    return "ok", 200


def manejar_cierre_conversacion(user_id, msg, conversation_id, conversations, supabase):
    final = generar_analisis_final_openai(conversations[user_id])
    lead_score = clasificar_lead(conversations[user_id], conversation_id)
    actualizar_perfil_comportamiento_usuario(user_id, conversation_id)
    guardar_resumen_usuario(user_id, conversation_id)

    supabase.from_("conversation_history").update({
        "final_analysis": final,
        "status": "finalizado",
        "lead_score": lead_score,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }).eq("conversation_id", conversation_id).execute()

    if lead_score in ["calificado", "medio"]:
        actualizar_etapa_conversacion(conversation_id, "seguimiento")
    elif lead_score == "no_calificado":
        actualizar_etapa_conversacion(conversation_id, "no_calificado")

    if lead_score in ["calificado", "medio"]:
        guardar_frase_conversion(user_id, msg, tipo="lead_calificado")
        registrar_kpi_evento(
            conversation_id=conversation_id,
            tipo_evento="lead_calificado",
            user_id=user_id
        )

    logging.info(f"📊 Lead clasificado como {lead_score} y etapa registrada.")
    return lead_score


def manejar_mensaje_promocional(user_id, mensaje_normalizado, conversation_id=None):
    palabras_clave = ["promocion", "promo", "descuento", "oferta", "rebaja"]
    if any(palabra in mensaje_normalizado for palabra in palabras_clave):
        archivos = obtener_ultimos_archivos(user_id)
        for archivo in archivos:
            texto_extraido = archivo.get("embedding_vector", "")
            if texto_extraido:
                respuesta = generar_respuesta_promocion(texto_extraido)
                if respuesta:
                    send_and_log(user_id, respuesta, conversation_id, tipo="text")
                    return True
    return False

def analizar_mensaje_individual(msg, message_id):
    try:
        single_analysis = generar_analisis_mensaje_unico(msg)
        if single_analysis:
            supabase.from_("interaction_history").update({
                "analysis_result": single_analysis
            }).eq("id", message_id).execute()

            embedding_response = client.embeddings.create(
                model="text-embedding-ada-002",
                input=msg
            )
            embedding_vector = embedding_response.data[0].embedding

            supabase.from_("interaction_history").update({
                "embedding_vector": embedding_vector
            }).eq("id", message_id).execute()
    except Exception as e:
        logging.error(f"❌ Error en análisis incremental: {e}")

def verificar_y_guardar_calificacion(user_id, msg, conversation_id):
    try:
        etapa_actual = supabase.from_("conversation_history")\
            .select("etapa_actual")\
            .eq("conversation_id", conversation_id)\
            .limit(1).execute()

        if etapa_actual.data and etapa_actual.data[0]["etapa_actual"] == "esperando_calificacion":
            logging.info("📝 Usuario está en etapa de calificación, intentando procesar...")

            # Usa GPT para extraer calificación + comentario
            analisis = client.chat.completions.create(
                model="gpt-3.5-turbo",
                temperature=0.3,
                messages=[
                    {"role": "system", "content": "Extrae una calificacion del 1 al 5 y un comentario (si existe) del siguiente texto. Formato: Calificacion: 4\nComentario: Excelente trato.\nSi no hay comentario, escribe 'Comentario: null'"},
                    {"role": "user", "content": msg}
                ]
            )
            salida = analisis.choices[0].message.content.strip()
            match = re.search(r"Calificacion:\s*(\d)\s*Comentario:\s*(.*)", salida)

            if match:
                calificacion = int(match.group(1))
                comentario = match.group(2).strip()
                comentario = None if comentario.lower() == "null" else comentario

                # Buscar última reserva del usuario
                reserva = supabase.from_("reservaciones")\
                    .select("id").eq("user_id", user_id)\
                    .order("fecha_reserva", desc=True).limit(1).execute()

                if reserva.data:
                    reserva_id = reserva.data[0]["id"]
                    supabase.from_("calificaciones").insert({
                        "reserva_id": reserva_id,
                        "user_id": user_id,
                        "calificacion": calificacion,
                        "comentario": comentario,
                        "canal": "whatsapp"
                    }).execute()

                    # Enviar respuesta de agradecimiento
                    send_and_log(
                        user_id,
                        "🙏 ¡Gracias por tu calificación! Nos ayuda a mejorar continuamente.",
                        conversation_id
                    )

                    # Actualizar etapa
                    supabase.from_("conversation_history").update({
                        "etapa_actual": "calificacion_recibida"
                    }).eq("conversation_id", conversation_id).execute()
                    return True
            else:
                send_and_log(
                    user_id,
                    "⚠️ No pude entender bien tu calificación. Por favor usa el formato:\n\nCalificación: 5\nComentario: Excelente atención.",
                    conversation_id
                )
                return True

        return False

    except Exception as e:
        logging.error(f"❌ Error verificando calificación: {e}")
        return False

def obtener_perfil_comportamiento_usuario(user_id):
    try:
        perfil = supabase.from_("user_behavior_profile")\
            .select("*")\
            .eq("user_id", user_id)\
            .limit(1)\
            .execute()
        return perfil.data[0] if perfil.data else None
    except Exception as e:
        logging.error(f"❌ Error obteniendo perfil de comportamiento: {e}")
        return None

def get_config_fabrica():
    plantilla = BOT_PLANTILLAS["generico"]
    return {
        "bot_type": ["generico"],
        "tipo_negocio": "generico",
        "ubicacion": "",
        "latitud": None,
        "longitud": None,
        "contexto": "",
        "tipo_reservas": "único_horario",
        "negocio": "generico",
        "requiere_zona": False,
        "zonas_permitidas": [],
        "nombre_bot": "Asistente",
        "prompt": plantilla["prompt"],
        "usa_embeddings": plantilla.get("usa_embeddings", False)
    }


def generar_respuesta_openai(mensaje, prompt_base, temperature=0.7, model="gpt-3.5-turbo"):
    """
    Genera una respuesta usando OpenAI GPT con un prompt base.

    Args:
        mensaje (str): Mensaje recibido del usuario.
        prompt_base (str): Instrucción base para el asistente.
        temperature (float): Creatividad de la respuesta (0.0-1.0).
        model (str): Modelo de OpenAI a usar.

    Returns:
        str: Respuesta generada por la IA.
    """
    try:
        # Arma el historial para GPT (puedes ajustar el formato según tus necesidades)
        mensajes = [
            {"role": "system", "content": prompt_base},
            {"role": "user", "content": mensaje}
        ]

        # Si usas la librería openai v1.x:
        response = client.chat.completions.create(
            model=model,
            messages=mensajes,
            temperature=temperature,
            max_tokens=300
        )
        texto = response.choices[0].message.content.strip()
        return texto

    except Exception as e:
        logging.error(f"❌ Error generando respuesta OpenAI: {e}")
        return "Perdón, en este momento no puedo responder a tu pregunta. ¿Puedes intentar de nuevo más tarde?"

app = Flask(__name__)
CORS(app)
@app.route("/whatsapp", methods=['POST'])
def webhook():
    if not request.form:
        logging.warning("⚠️ Payload vacío o no enviado como form-data")
        return "Se esperaba form-data (Twilio)", 400

    try:
        ya_respondio = False

        msg = request.form.get("Body", "").strip()
        sender = request.form.get("From", "").replace("whatsapp:", "")
        user_id = sender
        now = datetime.now()

        # ⚠️ Validar mensaje y remitente
        if not sender or not msg:
            logging.warning("⚠️ Faltan datos esenciales en el mensaje recibido.")
            return "Mensaje incompleto", 200


        # Verificar si hay estado pendiente de confirmación o zona
        if manejar_confirmacion_o_zona(user_id, msg):
           return "ok", 200
        
        config = obtener_configuracion_bot()
        tipo_negocio = config.get("tipo_negocio", "generico")
          #Obtener o crear conversation_id primero
        conversation_id = guardar_conversacion_si_no_existe(user_id, sender, tipo_negocio)

        # 1️⃣ Intención directa: ubicación
        if detectar_intencion_directa(msg, "ubicacion"):
            ubicacion = config.get("ubicacion", "📍 Ubicación no configurada.")
            enviar_ubicacion(
                usuario_id=user_id,
                ubicacion=ubicacion,
                lat=config.get("latitud"),
                lng=config.get("longitud"),
                conversation_id=conversation_id,
                config=config  # Puedes omitir si no lo usas
            )
            return "ok", 200

        
        # 🔁 Aplicar buffer inteligente
        msg = procesar_buffer_usuario(user_id, msg)
        
        logging.info(f"📩 Mensaje recibido de {sender}: {msg}")
        logging.info(f"📁 Registrando conversación con tipo_negocio: {tipo_negocio}")
        
        # Inicializar historial si no existe
        if user_id not in conversations:
            conversations[user_id] = []

            # Saludo inicial personalizado
            nombre_empresa = config.get("nombre_bot", "tu empresa")
            saludo_inicial = (
                f"👋 ¡Hola! Soy AIDANA, tu asistente virtual de confianza en {nombre_empresa}.\n\n"
                f"¿En qué puedo ayudarte hoy?\n"
                f"💬 Puedes preguntarme por:\n"
                f"• Precios especiales\n"
                f"• Modelos disponibles\n"
                f"• Planes de financiamiento\n"
                f"• Cotizaciones de seminuevos\n"
                f"• Agendar una prueba de manejo\n"
            )
            # Validar si ya se envió el saludo anteriormente
            saludo_check = supabase.from_("conversation_history")\
                .select("primer_saludo_enviado")\
                .eq("conversation_id", conversation_id)\
                .limit(1)\
                .execute()
            
            if not saludo_check.data or not saludo_check.data[0]["primer_saludo_enviado"]:
                conversations[user_id].append({"role": "assistant", "content": saludo_inicial})
                supabase.from_("conversation_history").update({
                    "primer_saludo_enviado": True
                }).eq("conversation_id", conversation_id).execute()
        ultimo_mensaje[user_id] = now
        conversations[user_id].append({"role": "user", "content": msg})
        # Limitar historial a 7 mensajes como máximo
        if len(conversations[user_id]) > 40:
            conversations[user_id] = conversations[user_id][-40:]


        # Intervención humana
        intervencion = supabase.from_("conversation_history")\
            .select("is_human")\
            .eq("conversation_id", conversation_id)\
            .limit(1)\
            .execute()
        if intervencion.data and intervencion.data[0]["is_human"]:
            guardar_mensaje_conversacion(conversation_id, msg, sender="user", tipo="text")
            return "ok", 200

        message_id = guardar_mensaje_conversacion(conversation_id, msg, sender="user", tipo="text")
    

        etapa_actual = supabase.from_("conversation_history")\
            .select("etapa_actual")\
            .eq("conversation_id", conversation_id)\
            .limit(1).execute()

        if verificar_y_guardar_calificacion(user_id, msg, conversation_id):
            return "ok", 200
        # 3️⃣ Aquí va tu bloque de cierre de conversación
        resultado_cierre = detectar_cierre_conversacion(user_id, msg, ultimo_mensaje)
        if resultado_cierre == "cerrar_ya":
            manejar_cierre_conversacion(user_id, msg, conversation_id, conversations, supabase)
            return responder_y_salir(sender, "✅ ¡Gracias por contactarnos! Conversación finalizada. Si necesitas algo más, escribe de nuevo.")

        # Media (imágenes, archivos, etc.)
        num_media = int(request.form.get("NumMedia", 0))
        if num_media > 0:
            for i in range(num_media):
                media_url = request.form.get(f"MediaUrl{i}")
                media_type = request.form.get(f"MediaContentType{i}")
                tipo_archivo = "image" if media_type.startswith("image/") else "video" if media_type.startswith("video/") else "file"
                supabase.from_("interaction_history").insert({
                    "conversation_id": conversation_id,
                    "sender_role": "user",
                    "message_type": tipo_archivo,
                    "file_url": media_url,
                    "file_type": media_type,
                    "start_time": datetime.now(timezone.utc).isoformat()
                }).execute()
                logging.info(f"📎 Archivo recibido ({media_type}): {media_url}")

        # Lead score
        reevaluar_lead_score_dinamico(conversation_id, conversations[user_id], user_id)

        # 🔤 Normalizar mensaje si aún no existe
        mensaje_normalizado = ''.join(
            c for c in unicodedata.normalize('NFD', msg.lower())
            if unicodedata.category(c) != 'Mn'
        )
        logging.info(f"🧭 Mensaje normalizado: {mensaje_normalizado}")
        # 🔎 Si el mensaje parece relacionado con promociones
        if manejar_mensaje_promocional(user_id, mensaje_normalizado):
            ya_respondio = True
            return "ok", 200

        if message_id:
            analizar_mensaje_individual(msg, message_id)



        # 🧠 Cargar config de plantilla
        config = obtener_configuracion_bot(conversation_id)
        # 🧠 Detectar intención del mensaje
        intencion_detectada = detectar_intencion_comercial_o_reserva(msg, config.get("contexto", ""))
        logging.info(f"🧠 Intención detectada: {intencion_detectada}")

        nombre_bot = config.get("nombre_bot", "Asistente")
        if user_id in ultimo_mensaje and now - ultimo_mensaje[user_id] > timedelta(minutes=15):
           conversations[user_id].append({"role": "assistant", "content": f"Hola de nuevo, soy {nombre_bot}. Retomemos tu conversación anterior..."})
        ultimo_mensaje[user_id] = now  # actualiza siempre después del saludo

        funciones = obtener_funciones_disponibles(config["bot_type"])


        # Reintento con embeddings si GPT no detectó nada
        if intencion_detectada == "ninguno":
            # Probar con embeddings (si hay)
            if analizar_intencion_con_embeddings(msg, tipo="ventas", negocio=config.get("negocio", "generico")):
                intencion_detectada = "ventas"
                gestion_ok = gestionar_reserva(user_id, msg, sender, config, conversation_id)
                if gestion_ok:
                    return "ok", 200
                else:
                    # Aquí puedes mandar el prompt base de asistente AI
                    config_fabrica = get_config_fabrica()
                    send_and_log(
                        sender, 
                        "¡Hola! Soy tu asistente virtual. ¿En qué puedo ayudarte? Puedes preguntarme cualquier cosa, aunque no tenga datos específicos del negocio todavía.", 
                        conversation_id, 
                        tipo="text"
                    )
                    return "ok", 200
            else:
                 # Fallback TOTAL: Reset config y contesta usando OpenAI
                config_fabrica = get_config_fabrica()
                respuesta_ai = generar_respuesta_openai(msg, config_fabrica["prompt"])
                send_and_log(sender, respuesta_ai, conversation_id, tipo="text")
                return "ok", 200

            

        # Registrar intención detectada como KPI
        registrar_kpi_evento(
            conversation_id=conversation_id,
            tipo_evento=f"intencion_{intencion_detectada}",
            user_id=user_id
        )


        # 🧠 Si hay función de agendar, y detecta reserva o ambos
        if "agendar_reserva" in funciones and intencion_detectada in ["reserva", "ambos"]:
            # Verificamos si el usuario dio alguna pista clara de fecha/hora
            hay_fecha_hora = re.search(
                r"(mañana|hoy|lunes|martes|miércoles|jueves|viernes|sábado|domingo|\d{1,2}(:|\s)?(am|pm)?|\d{2}:\d{2})",
                msg, re.IGNORECASE
            )

            if hay_fecha_hora:
                # Si detecta pista, lanza reserva directamente
                gestion_ok = gestionar_reserva(user_id, msg, sender, config, conversation_id)
                if gestion_ok:
                    return "ok", 200
                else:
                     # Si no hay pista, solo responde con amabilidad y se pone a disposición
                     send_and_log(sender, f"¡Hola! Soy AIDANA. ¿En qué puedo ayudarte hoy?", conversation_id, tipo="text")
                     ya_respondio = True
                     return "ok", 200


        # Preparar mensajes para OpenAI
        historial = obtener_historial_contexto(user_id)  # o más si deseas más contexto
        prompt_sistema = config["prompt"]

        # Si no hay archivo, elimina la parte de archivos del prompt
        num_media = int(request.form.get("NumMedia", 0))
        if num_media == 0:
            prompt_sistema = re.sub(r"- .*archivos.*\n?", "", prompt_sistema, flags=re.IGNORECASE)

        messages = [{"role": "system", "content": prompt_sistema}] + historial

        # --- 💡 PERFIL DE COMPORTAMIENTO ---
        perfil = obtener_perfil_comportamiento_usuario(user_id)  # Debes crear esta función tipo SELECT * ...
        if perfil:
            perfil_texto = f"""
        Perfil histórico del usuario:
        Lead score promedio: {perfil.get('lead_score_promedio', 'N/A')}
        Frases de interés: {perfil.get('frases_interes', 'N/A')}
        Estilo: {perfil.get('estilo_mensaje', 'N/A')}
        Días activo: {perfil.get('dias_activo', 'N/A')}
        """
            # Lo puedes insertar en segundo lugar o como system context
            messages.insert(1, {"role": "system", "content": perfil_texto})
        # 🧠 Recuperar memoria si existe
        memoria_usuario = obtener_memoria_usuario(user_id, cuantos=3)
        if memoria_usuario:
            try:
                # 🧠 Embedding del resumen
                embedding_resumen = client.embeddings.create(
                    model="text-embedding-ada-002",
                    input=memoria_usuario
                ).data[0].embedding

                # 🧠 Embedding del nuevo mensaje
                embedding_usuario = client.embeddings.create(
                    model="text-embedding-ada-002",
                    input=msg
                ).data[0].embedding

                # 🧮 Calcular similitud
                similitud = cosine_similarity(embedding_resumen, embedding_usuario)

                logging.info(f"📊 Similitud con memoria previa: {similitud:.2f}")

                if similitud > 0.65:
                    messages.insert(1, {"role": "system", "content": f"Contexto previo del usuario:\n{memoria_usuario}"})
                    logging.info("🧠 Memoria insertada como contexto útil")
                else:
                    logging.info("🧠 Memoria omitida (similitud baja)")

            except Exception as e:
                logging.error(f"❌ Error evaluando similitud con memoria: {e}")
        
        # Agregar fragmentos si usa embeddings
        if config["usa_embeddings"] and intencion_detectada in ["reserva", "venta", "ambos"]:
            fragmentos = buscar_fragmento_relevante(msg, top_k=10000) 
            bloques = construir_fragmento_resumido_por_tokens(fragmentos, max_tokens=7000, modelo="gpt-4")
            for i, bloque in enumerate(bloques):
                logging.info(f"🧠 Bloque {i+1} insertado ({len(bloque)} chars)")
                messages.insert(2 + i, {"role": "system", "content": bloque})

        gpt_response = client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            temperature=0.6,
            max_tokens=500
        )
        bot_reply = gpt_response.choices[0].message.content.strip()
        # ⚠️ Elimina líneas internas antes de enviar la respuesta
        bot_reply = re.sub(r"(?i)^calificacion:.*$", "", bot_reply, flags=re.MULTILINE).strip()
        bot_reply = re.sub(r"(?i)^comentario:.*$", "", bot_reply, flags=re.MULTILINE).strip()
        # Si no se generó una respuesta clara y la intención era reserva
        if (not bot_reply or bot_reply.lower().startswith("lo siento")) and intencion_detectada in ["reserva", "ambos"]:
            logging.warning("⚠️ GPT no generó una respuesta útil. Forzando flujo de reserva.")
            return responder_y_salir(sender, "🗓️ Para agendar una demo, por favor dime qué día y hora te gustaría. También indícame si prefieres Zoom o presencial.")

        if ya_respondio:
            return "ok", 200

        if len(conversations[user_id]) > 7:
            conversations[user_id] = conversations[user_id][-7:]
        
        if bot_reply:
            guardar_mensaje_conversacion(conversation_id, bot_reply, sender="bot", tipo="text")
            # ✅ Enviar respuesta al usuario
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=f"whatsapp:{user_id}",
                body=bot_reply
            )
            logging.info(f"📤 Mensaje enviado a {user_id}: {bot_reply}")

        # Análisis cada 4 mensajes
        if len(conversations[user_id]) % 4 == 0:
            insight = generar_analisis_incremental(conversations[user_id][-6:])
            if insight:
                supabase.from_("conversation_history").update({
                    "analysis_incremental": insight
                }).eq("conversation_id", conversation_id).execute()

        resultado_cierre = detectar_cierre_conversacion(user_id, msg, ultimo_mensaje)
        if resultado_cierre == "cerrar_ya":
            manejar_cierre_conversacion(user_id, msg, conversation_id, conversations, supabase)
            return responder_y_salir(sender, "✅ ¡Gracias por contactarnos! Conversación finalizada. Si necesitas algo más, escribe de nuevo.")


    except Exception as e:
        logging.error(f"❌ Error procesando mensaje: {e}")
        logging.exception("❌ Error general procesando mensaje:")
        return "Ocurrió un error, intenta nuevamente.", 200
    # ✅ Agrega esto fuera del try-except como fallback:
    return "ok", 200

app.add_url_rule("/actualizar_estado", view_func=actualizar_estado, methods=["POST", "OPTIONS"])
threading.Thread(target=seguimiento_leads_silenciosos, daemon=True).start()
if __name__ == '__main__':
    import citas_bot
    citas_bot.iniciar_citas_bot()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)

