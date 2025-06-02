import os
import logging
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from flask import Flask, request
from dotenv import load_dotenv
from twilio.rest import Client as TwilioClient
from supabase import create_client, Client
from openai import OpenAI
from dateutil import parser
from zoneinfo import ZoneInfo
import calendar
from flask_cors import CORS
import re
import hashlib

# === Configuración ===
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

MEXICO_TZ = ZoneInfo("America/Mexico_City")
ahora_mx = datetime.now(MEXICO_TZ)
fecha_actual_str = ahora_mx.strftime("%A %d de %B de %Y")
hora_actual_str = ahora_mx.strftime("%H:%M")

client = OpenAI(api_key=OPENAI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH)

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# === Funciones auxiliares ===
def enviar_respuesta(destino, texto):
    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        to=f"whatsapp:{destino}",
        body=texto
    )

def convertir_dia_a_fecha(dia):
    dias_semana = {
        "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3,
        "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
    }
    hoy = datetime.now(MEXICO_TZ)
    dia = dia.lower()
    target_dia = dias_semana.get(dia)
    if target_dia is None:
        return None
    dias_para_ese_dia = (target_dia - hoy.weekday() + 7) % 7
    dias_para_ese_dia = dias_para_ese_dia or 7
    return (hoy + timedelta(days=dias_para_ese_dia)).date().isoformat()

def generar_mensaje_confirmacion(config, fecha_completa, servicio="demo"):
    nombre_bot = config.get("nombre_bot", "tu asistente")
    negocio = config.get("negocio", "tu servicio")

    # Asegúrate de que sea datetime antes de usar strftime
    if isinstance(fecha_completa, str):
        fecha_completa = datetime.fromisoformat(fecha_completa)
    # 🔥 Forzar conversión a zona horaria CDMX ANTES de mostrar
    fecha_mx = fecha_completa.astimezone(MEXICO_TZ)

    mensaje = (
        f"📅 ¡Listo! {nombre_bot} acabo {servicio.lower()} para el "
        f"{fecha_mx.strftime('%A %d de %B a las %H:%M')} hora CDMX.\n\n"
        f"Recibirás un recordatorio antes. Si necesitas mover la cita o tienes más dudas, solo dime."
    )
    return mensaje




def guardar_reserva(user_id, servicio, fecha_completa, sender, config=None, conversation_id=None, zona="no_aplica", cliente_nombre="Cliente WhatsApp"):
    datos = {
        "user_id": user_id,
        "cliente_nombre": cliente_nombre,
        "whatsapp_number": sender,
        "servicio": servicio,
        "zona": zona,
        "fecha_reserva": fecha_completa.astimezone(timezone.utc).isoformat() if isinstance(fecha_completa, datetime) else fecha_completa,
        "estado": "pendiente",
        "conversation_id": conversation_id
    }
    try:
        resultado = supabase.from_("reservaciones").insert(datos).execute()
        logging.info(f"📝 Reserva guardada correctamente: {resultado}")

        mensaje_confirmacion = generar_mensaje_confirmacion(config or {}, fecha_completa, servicio)
        responder_y_registrar(sender, mensaje_confirmacion, conversation_id)

        return True
    except Exception as e:
        logging.error(f"❌ Error al guardar reserva: {e}")
        responder_y_registrar(sender, "❌ No pude agendar tu cita. Intenta de nuevo.")
        return False
    
def guardar_estado_pendiente(user_id, data):
    data["user_id"] = user_id
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    supabase.from_("reserva_pendiente").upsert(data).execute()

def obtener_estado_pendiente(user_id):
    resultado = supabase.from_("reserva_pendiente").select("*").eq("user_id", user_id).limit(1).execute()
    return resultado.data[0] if resultado.data else None

def eliminar_estado_pendiente(user_id):
    supabase.from_("reserva_pendiente").delete().eq("user_id", user_id).execute()



# === Core ===
def gestionar_reserva(user_id, msg, sender, config=None, conversation_id=None):
    try:
        # 1️⃣ Inferir servicio si no se menciona
        try:
            inferencia = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {
                        "role": "system",
                        "content": "Dado el siguiente mensaje, extrae el tipo de servicio o motivo de la cita. "
                "Ejemplos: masaje, demo, consulta, comida, presentación, etc. "
                "Si no se menciona explícitamente, devuelve 'General' o el servicio principal de este negocio."
                    },
                    {"role": "user", "content": msg}
                ],
                max_tokens=10
            )
            servicio_detectado = inferencia.choices[0].message.content.strip() or (config.get("negocio", "General") if config else "General")
            # 👇 Agrega este log para ver qué detectó
            logging.info(f"🟦 Servicio detectado por OpenAI: {servicio_detectado} | Mensaje original: {msg}")
        except:
            servicio_detectado = "General"
            logging.warning(f"⚠️ Error detectando servicio: {e}. Se usó 'General'.")

        # 2️⃣ Extraer fecha y hora
        respuesta = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {
                    "role": "system",
                    "content": f"""
Eres un asistente que agenda citas. Hoy es {fecha_actual_str} y la hora actual es {hora_actual_str} hora de Ciudad de México.

Tu tarea es extraer 3 elementos de un mensaje de usuario:
1. El servicio (si se menciona, o usa 'General' como valor por defecto).
2. La fecha (acepta cosas como 'mañana', 'sábado', o fechas explícitas).
3. La hora (en cualquier formato: '11', '11 am', '6 de la tarde', etc).

Devuelve los datos en formato JSON con fecha ISO (YYYY-MM-DD) y hora (HH:MM). Si no entiendes alguno, responde con null.
"""
                },
                {"role": "user", "content": msg}
            ],
            functions=[{
                "name": "crear_reserva",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "servicio": {"type": "string"},
                        "fecha": {"type": "string"},
                        "hora": {"type": "string"}
                    },
                    "required": ["fecha", "hora"]
                }
            }],
            function_call={"name": "crear_reserva"}
        )

        args = json.loads(respuesta.choices[0].message.function_call.arguments)
        if not args.get("fecha") or not args.get("hora"):
            responder_y_registrar(sender, "❌ No entendí bien tu solicitud. ¿Puedes decirme el día y la hora exactos?", conversation_id)
            return False

        fecha_raw = args["fecha"].strip().lower()
        hora_raw = args["hora"].strip()
        servicio = args.get("servicio") or servicio_detectado or "General"

        if fecha_raw == "mañana":
            fecha = (datetime.now(MEXICO_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        elif fecha_raw == "hoy":
            fecha = datetime.now(MEXICO_TZ).strftime("%Y-%m-%d")
        elif fecha_raw in ["lunes", "martes", "miércoles", "miercoles", "jueves", "viernes", "sábado", "sabado", "domingo"]:
            fecha = convertir_dia_a_fecha(fecha_raw)
        else:
            try:
                fecha = parser.parse(fecha_raw, fuzzy=True).date().isoformat()
            except Exception:
                responder_y_registrar(sender, "❌ No entendí bien la fecha. ¿Puedes decir algo como 'sábado a las 10am'?", conversation_id)
                return False

        try:
            fecha_completa = parser.parse(f"{fecha} {hora_raw}")
            fecha_completa = fecha_completa.replace(tzinfo=MEXICO_TZ) if fecha_completa.tzinfo is None else fecha_completa.astimezone(MEXICO_TZ)
        except Exception:
            responder_y_registrar(sender, "❌ No entendí bien la hora. ¿Puedes decirla como '4pm' o '16:00'?", conversation_id)
            return False

        if fecha_completa <= datetime.now(MEXICO_TZ):
            responder_y_registrar(sender, "⚠️ La fecha y hora que mencionaste ya pasó. ¿Puedes darme una hora futura?", conversation_id)
            return False

        # 3️⃣ Validación de horario único si aplica
        unico_horario = config.get("unico_horario", False) if config else False
        if unico_horario:
            conflicto = supabase.from_("reservaciones")\
                .select("id")\
                .eq("fecha_reserva", fecha_completa.isoformat())\
                .neq("estado", "cancelada")\
                .execute()

            if conflicto.data:
                responder_y_registrar(sender, "⚠️ Lo siento, ese horario ya está reservado. ¿Quieres intentar con otro?", conversation_id)
                return False

        # 4️⃣ Zona si aplica
        requiere_zona = config.get("requiere_zona", False) if config else False
        if requiere_zona:
            guardar_estado_pendiente(user_id, {
                "fecha_completa": fecha_completa.isoformat(),
                "servicio": servicio,
                "sender": sender,
                "config": config,
                "conversation_id": conversation_id,
                "estado": "esperando_zona"
            })
            responder_y_registrar(sender, "🍽️ ¿En qué zona prefieres? Salón, Terraza o VIP.", conversation_id)
            return True

        # ✅ Siempre mostrar la hora en MEXICO_TZ al usuario
        fecha_local = fecha_completa.astimezone(MEXICO_TZ)
        fecha_texto = fecha_local.strftime("%A %d de %B a las %H:%M")
        guardar_estado_pendiente(user_id, {
            "fecha_completa": fecha_completa.isoformat(),
            "servicio": servicio,
            "sender": sender,
            "config": config,
            "conversation_id": conversation_id,
            "estado": "esperando_confirmacion"
        })
        responder_y_registrar(sender, f"""📝 Confirmemos tu cita:

• Servicio: {servicio}
• Día: {fecha_texto}

¿Confirmas que estos datos son correctos?
Responde *sí* para guardar tu cita.""", conversation_id)
        return True

    except Exception as e:
        logging.error(f"❌ Error procesando reserva: {e}")
        responder_y_registrar(sender, "❌ No entendí bien tu mensaje. Por favor intenta de nuevo.", conversation_id)
        return False



def extraer_datos_contacto(msg):
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Extrae el nombre completo y correo del siguiente texto. Devuelve JSON con campos 'nombre' y 'correo'. Si alguno no está, devuelve null."},
                {"role": "user", "content": msg}
            ]
        )
        datos = json.loads(response.choices[0].message.content.strip())
        return datos
    except:
        return {"nombre": None, "correo": None}


def manejar_confirmacion_o_zona(user_id, msg):
    estado = obtener_estado_pendiente(user_id)
    if not estado:
        return False

    msg_normalizado = msg.lower().strip()
    conversation_id = estado.get("conversation_id")

    # Paso 1: Si está esperando zona
    if estado["estado"] == "esperando_zona":
        zona = msg_normalizado.capitalize()

        config = estado.get("config", {})
        zonas_validas = config.get("zonas_permitidas", ["Salón", "Terraza", "VIP"])

        if zona.lower() not in [z.lower() for z in zonas_validas]:
            responder_y_registrar(
                estado["sender"],
                f"⚠️ Esa zona no es válida. Opciones disponibles: {', '.join(zonas_validas)}",
                conversation_id
            )
            return True

        estado["zona"] = zona
        estado["estado"] = "esperando_confirmacion"
        guardar_estado_pendiente(user_id, estado)

        fecha_completa = estado["fecha_completa"]
        if isinstance(fecha_completa, str):
            fecha_completa = datetime.fromisoformat(fecha_completa)
        fecha_local = fecha_completa.astimezone(MEXICO_TZ)

        fecha_texto = fecha_local.strftime("%A %d de %B a las %H:%M")
        responder_y_registrar(estado["sender"], f"""📝 Confirmemos tu cita:

• Servicio: {estado['servicio']}
• Día: {fecha_texto}
• Zona: {zona}

¿Confirmas que estos datos son correctos?
Responde *sí* para guardar tu cita.""", conversation_id)
        return True

    # Paso 2: Confirmación → verificar si faltan nombre/correo
    if estado["estado"] == "esperando_confirmacion" and msg_normalizado in ["sí", "si", "confirmo", "ok"]:
        if not estado.get("cliente_nombre") or not estado.get("correo"):
            estado["estado"] = "esperando_datos"
            guardar_estado_pendiente(user_id, estado)
            responder_y_registrar(estado["sender"], "🧾 Para confirmar tu cita necesito tu *nombre completo* y tu *correo electrónico*. Por favor escríbelos juntos.", conversation_id)
            return True

        zona = estado.get("zona", "no_aplica")
        guardar_reserva(
            user_id=user_id,
            servicio=estado["servicio"],
            fecha_completa=estado["fecha_completa"],
            sender=estado["sender"],
            config=estado["config"],
            conversation_id=conversation_id,
            zona=zona,
            cliente_nombre=estado.get("cliente_nombre", "Cliente WhatsApp")
        )
        eliminar_estado_pendiente(user_id)
        return True

    # Paso 3: Capturar datos de contacto si los está esperando
    if estado["estado"] == "esperando_datos":
        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Extrae nombre y correo del siguiente texto. Devuelve JSON: {'nombre': '', 'correo': ''}. Si no hay alguno, pon null."},
                    {"role": "user", "content": msg}
                ]
            )
            datos = json.loads(response.choices[0].message.content.strip())
            nombre = datos.get("nombre")
            correo = datos.get("correo")

            if not nombre or not correo:
                responder_y_registrar(estado["sender"], "❌ No logré entender bien tu *nombre* y *correo*. Por favor escríbelos así:\nJuan Pérez - juan@email.com", conversation_id)
                return True
            
            if not correo_valido(correo):
                responder_y_registrar(estado["sender"], "❌ Asegúrate de escribir correctamente tu nombre y un correo válido (por ejemplo: juan@email.com).", conversation_id)
                return True

            estado["cliente_nombre"] = nombre
            estado["correo"] = correo
            estado["estado"] = "esperando_confirmacion"
            guardar_estado_pendiente(user_id, estado)

            fecha_completa = estado["fecha_completa"]
            if isinstance(fecha_completa, str):
                fecha_completa = datetime.fromisoformat(fecha_completa)
            fecha_local = fecha_completa.astimezone(MEXICO_TZ)

            fecha_texto = fecha_local.strftime("%A %d de %B a las %H:%M")
            responder_y_registrar(estado["sender"], f"""📝 Confirmemos tu cita:

• Servicio: {estado['servicio']}
• Día: {fecha_texto}
• Zona: {estado.get("zona", "no_aplica")}
• Nombre: {nombre}
• Correo: {correo}

¿Confirmas que estos datos son correctos?
Responde *sí* para guardar tu cita.""", conversation_id)
            return True

        except Exception as e:
            logging.error(f"❌ Error extrayendo datos de contacto: {e}")
            responder_y_registrar(estado["sender"], "⚠️ Ocurrió un error al procesar tus datos. Por favor intenta de nuevo escribiendo tu *nombre* y *correo electrónico*.", conversation_id)
            return True

    return False




def extraer_datos_contacto(msg):
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Extrae el nombre completo y correo del siguiente texto. Devuelve JSON con campos 'nombre' y 'correo'. Si alguno no está, devuelve null."},
                {"role": "user", "content": msg}
            ]
        )
        datos = json.loads(response.choices[0].message.content.strip())
        return datos
    except:
        return {"nombre": None, "correo": None}


# === Recordatorios ===
def confirmar_asistencia(user_id, msg):
    msg_normalizado = msg.lower()

    # Obtener nombre del negocio desde configuración
    nombre_negocio = "nuestro negocio"
    try:
        config = supabase.from_("bot_configuracion")\
            .select("negocio")\
            .order("created_at", desc=True).limit(1).execute()
        if config.data:
            nombre_negocio = config.data[0]["negocio"]
    except:
        pass

    # Obtener reserva más reciente en estado "esperando_confirmacion"
    reserva = supabase.from_("reservaciones")\
        .select("id, conversation_id")\
        .eq("user_id", user_id)\
        .eq("estado", "esperando_confirmacion")\
        .order("fecha_reserva", desc=True)\
        .limit(1)\
        .execute()

    conversation_id = None
    if reserva.data:
        conversation_id = reserva.data[0]["conversation_id"]

    if msg_normalizado in ["sí asistí", "si asistí", "sí fui", "asistí", "fui"]:
        supabase.from_("reservaciones").update({
            "estado": "confirmada"
        }).eq("user_id", user_id).eq("estado", "esperando_confirmacion")\
          .order("fecha_reserva", desc=True).limit(1).execute()

        mensaje = f"✅ ¡Nos alegra que ya estés con nosotros en {nombre_negocio}! Si necesitas algo durante tu estancia, no dudes en escribirnos."
        responder_y_registrar(user_id, mensaje, conversation_id)
        return True

    elif msg_normalizado in ["no asistí", "no fui", "no pude ir"]:
        supabase.from_("reservaciones").update({
            "estado": "no_asistio"
        }).eq("user_id", user_id).eq("estado", "esperando_confirmacion")\
          .order("fecha_reserva", desc=True).limit(1).execute()

        responder_y_registrar(user_id, "📌 Gracias por informarnos. Esperamos verte en otra ocasión.", conversation_id)
        return True

    return False



# === Recordatorios ===
def enviar_recordatorios():
    while True:
        try:
            now = datetime.now(timezone.utc)
            margen = timedelta(minutes=5)

            # === Recordatorio 24h antes ===
            resultado_24h = supabase.from_("reservaciones")\
                .select("id, user_id, fecha_reserva, cliente_nombre, recordatorio_24h_enviado, conversation_id")\
                .eq("estado", "pendiente")\
                .eq("recordatorio_24h_enviado", False)\
                .execute()

            for r in resultado_24h.data or []:
                fecha_reserva = parse_fecha_supabase(r["fecha_reserva"])
                if not fecha_reserva:
                    continue

                diferencia = fecha_reserva - now
                if timedelta(hours=24) - margen <= diferencia <= timedelta(hours=24) + margen:
                    local = fecha_reserva.astimezone(MEXICO_TZ)
                    mensaje = f"⏰ Hola {r['cliente_nombre']}, te recuerdo que tienes una reserva mañana a las {local.strftime('%H:%M')} (hora CDMX). ¡Te esperamos!"

                    responder_y_registrar(r["user_id"], mensaje, conversation_id=r["conversation_id"])
                    supabase.from_("reservaciones").update({
                        "recordatorio_24h_enviado": True
                    }).eq("id", r["id"]).execute()
                    logging.info(f"🔔 Recordatorio 24h enviado a {r['user_id']}")

            # === Recordatorio 1h antes ===
            resultado_1h = supabase.from_("reservaciones")\
                .select("id, user_id, fecha_reserva, cliente_nombre, recordatorio_1h_enviado, conversation_id")\
                .eq("estado", "pendiente")\
                .eq("recordatorio_1h_enviado", False)\
                .execute()

            for r in resultado_1h.data or []:
                fecha_reserva = parse_fecha_supabase(r["fecha_reserva"])
                if not fecha_reserva:
                    continue

                diferencia = fecha_reserva - now
                if timedelta(hours=1) - margen <= diferencia <= timedelta(hours=1) + margen:
                    local = fecha_reserva.astimezone(MEXICO_TZ)
                    mensaje = f"🕒 ¡Hola {r['cliente_nombre']}! Tu cita es en 1 hora, a las {local.strftime('%H:%M')} (hora CDMX). ¡Nos vemos pronto!"

                    responder_y_registrar(r["user_id"], mensaje, conversation_id=r["conversation_id"])
                    supabase.from_("reservaciones").update({
                        "recordatorio_1h_enviado": True
                    }).eq("id", r["id"]).execute()
                    logging.info(f"🔔 Recordatorio 1h enviado a {r['user_id']}")

            # === Seguimiento 10-12 min después de la cita ===
            resultado_confirmadas = supabase.from_("reservaciones")\
                .select("id, user_id, cliente_nombre, fecha_reserva, estado, conversation_id")\
                .in_("estado", ["pendiente"])\
                .execute()

            config_negocio = supabase.from_("bot_configuracion")\
                .select("negocio, contexto, tipo_negocio")\
                .order("created_at", desc=True)\
                .limit(1).execute()

            tipo_negocio = config_negocio.data[0]["tipo_negocio"] if config_negocio.data else "generico"
            contexto_negocio = config_negocio.data[0].get("contexto", "") if config_negocio.data else ""

            for r in resultado_confirmadas.data or []:
                fecha_reserva = parse_fecha_supabase(r["fecha_reserva"])
                if not fecha_reserva:
                    continue

                diferencia = now - fecha_reserva
                if timedelta(minutes=10) <= diferencia <= timedelta(minutes=12):
                    local = fecha_reserva.astimezone(MEXICO_TZ)

                    # Mensaje con OpenAI según tipo_negocio
                    try:
                        prompt = f"""
Eres un asistente virtual que envía un *seguimiento suave* a un usuario 10 minutos después de su cita.

Contexto:
- Tipo de negocio: {tipo_negocio}
- Nombre del cliente: {r['cliente_nombre']}
- Hora de la cita: {local.strftime('%H:%M')}
- Tu objetivo es solo confirmar si asistió (sin ofrecer nada más).

Escribe un mensaje con emojis, cálido y natural, que diga algo como: "¿Pudiste asistir?" o "¿Todo fue bien?".
No vendas nada. No digas "te esperamos".
Solo busca confirmar la asistencia.
"""

                        completion = client.chat.completions.create(
                            model="gpt-3.5-turbo",
                            messages=[{"role": "system", "content": prompt.strip()}],
                            temperature=0.4,
                            max_tokens=80
                        )
                        mensaje = completion.choices[0].message.content.strip()

                    except Exception as e:
                        logging.warning(f"⚠️ OpenAI fallback: {e}")
                        mensaje = f"👋 Hola {r['cliente_nombre']}, ¿lograste asistir a tu cita de las {local.strftime('%H:%M')}?"

                    responder_y_registrar(r["user_id"], mensaje, conversation_id=r["conversation_id"])
                    supabase.from_("reservaciones").update({
                        "estado": "esperando_confirmacion"
                    }).eq("id", r["id"]).execute()

                    logging.info(f"✅ Seguimiento post-cita enviado a {r['user_id']}")

        except Exception as e:
            logging.error(f"❌ Error en recordatorios automáticos: {e}")

        time.sleep(900)  # 15 minutos




def enviar_encuesta_satisfaccion(user_id, reserva_id, nombre_bot="nuestro servicio"):
    try:
        mensaje = (
            f"🙏 ¡Gracias por visitarnos en {nombre_bot}!\n\n"
            f"¿Podrías calificarnos del 1 al 5 según tu experiencia?\n"
            f"También puedes añadir un comentario. Ejemplo:\n\n"
            f"Calificación: 5\nComentario: Excelente atención y servicio."
        )

        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{user_id}",
            body=mensaje
        )

        logging.info(f"📨 Encuesta de satisfacción enviada a {user_id} (reserva {reserva_id})")

        # ✅ Marca en conversation_history que estamos esperando calificación
        supabase.from_("conversation_history").update({
            "etapa_actual": "esperando_calificacion"
        }).eq("user_id", user_id).execute()

        return True

    except Exception as e:
        logging.error(f"❌ Error enviando encuesta de satisfacción: {e}")
        return False


def cron_encuesta_satisfaccion():
    while True:
        try:
            # Buscar reservas completadas sin encuesta enviada
            resultado = supabase.from_("reservaciones")\
                .select("id, user_id, cliente_nombre, conversation_id, encuesta_enviada")\
                .eq("estado", "completada")\
                .eq("encuesta_enviada", False)\
                .execute()

            for r in resultado.data or []:
                user_id = r["user_id"]
                reserva_id = r["id"]
                cliente = r["cliente_nombre"]
                conversation_id = r["conversation_id"]

                # Obtener nombre del negocio
                config = supabase.from_("bot_configuracion")\
                    .select("nombre_bot")\
                    .order("created_at", desc=True)\
                    .limit(1).execute()

                nombre_bot = config.data[0]["nombre_bot"] if config.data and config.data[0].get("nombre_bot") else "nuestro servicio"

                # Mensaje con OpenAI
                try:
                    prompt = f"""
Eres un asistente virtual que agradece al cliente por su visita al negocio "{nombre_bot}".

Escribe un mensaje corto, amable y natural con emojis, pidiendo que califique su experiencia del 1 al 5 y deje un comentario opcional. No suenes robótico.

Incluye ejemplo como:
Calificación: 5
Comentario: Excelente atención.
"""
                    completion = client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[{"role": "system", "content": prompt.strip()}],
                        temperature=0.4,
                        max_tokens=100
                    )
                    mensaje = completion.choices[0].message.content.strip()
                except Exception as e:
                    logging.warning(f"⚠️ OpenAI fallback: {e}")
                    mensaje = (
                        f"🙏 ¡Gracias por visitarnos en {nombre_bot}!\n\n"
                        f"¿Podrías calificarnos del 1 al 5 según tu experiencia?\n"
                        f"También puedes añadir un comentario. Ejemplo:\n\n"
                        f"Calificación: 5\nComentario: Excelente atención y servicio."
                    )

                # Enviar por WhatsApp
                twilio_client.messages.create(
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=f"whatsapp:{user_id}",
                    body=mensaje
                )

                # Registrar interacción
                responder_y_registrar(user_id, mensaje, conversation_id)

                supabase.from_("conversation_history").update({
                    "etapa_actual": "esperando_calificacion"
                }).eq("conversation_id", conversation_id).execute()

                # Marcar como enviada
                supabase.from_("reservaciones").update({
                    "encuesta_enviada": True
                }).eq("id", reserva_id).execute()
                

                logging.info(f"📨 Encuesta de satisfacción enviada a {user_id} (reserva {reserva_id})")

        except Exception as e:
            logging.error(f"❌ Error en cron de encuestas: {e}")

        time.sleep(900)  # Cada 15 minutos

def procesar_calificacion(user_id, msg):
    msg_normalizado = msg.strip().lower()

    try:
        # 1️⃣ Intentar extraer calificación con OpenAI
        prompt = f"""
El siguiente mensaje es una respuesta de un cliente después de una visita o servicio:

"{msg}"

Extrae la calificación (número del 1 al 5) y el comentario si lo hay.

Devuelve JSON como:
{{
  "calificacion": 5,
  "comentario": "Excelente servicio"
}}

Si no entiendes el mensaje, devuelve null.
"""
        completion = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": prompt.strip()}],
            max_tokens=50,
            temperature=0.3
        )

        resultado = json.loads(completion.choices[0].message.content.strip())
        calificacion = resultado.get("calificacion")
        comentario = resultado.get("comentario") or ""

        if not calificacion or not (1 <= int(calificacion) <= 5):
            return False

        # 2️⃣ Obtener la última reserva completada y con encuesta_enviada = true
        reserva = supabase.from_("reservaciones")\
            .select("id")\
            .eq("user_id", user_id)\
            .eq("encuesta_enviada", True)\
            .order("fecha_reserva", desc=True)\
            .limit(1).execute()

        if not reserva.data:
            logging.warning(f"⚠️ No se encontró reserva válida para calificación de {user_id}")
            return False

        reserva_id = reserva.data[0]["id"]

        # 3️⃣ Guardar en tabla calificaciones
        supabase.from_("calificaciones").insert({
            "reserva_id": reserva_id,
            "user_id": user_id,
            "calificacion": int(calificacion),
            "comentario": comentario.strip(),
            "canal": "whatsapp",
            "fecha": datetime.now(timezone.utc).isoformat()
        }).execute()

        logging.info(f"⭐ Calificación guardada para usuario {user_id} → {calificacion} | {comentario}")
        return True

    except Exception as e:
        logging.error(f"❌ Error al procesar calificación: {e}")
        return False


def correo_valido(correo):
    return re.match(r"[^@]+@[^@]+\.[^@]+", correo) is not None

def parse_fecha_supabase(fecha_str):
    """
    Convierte string ISO en datetime con zona horaria UTC.
    Si ya tiene zona, la respeta.
    """
    try:
        dt = datetime.fromisoformat(fecha_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception as e:
        logging.error(f"❌ Error al parsear fecha Supabase: {e}")
        return None




def responder_y_registrar(user_id, mensaje, conversation_id=None, tipo="text"):
    enviar_respuesta(user_id, mensaje)

    try:
        created_at = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
        message_hash = hashlib.sha256(f"{conversation_id}-{mensaje}-{created_at}".encode()).hexdigest()

        supabase.from_("interaction_history").upsert({
            "message_hash": message_hash,
            "bot_response": mensaje,
            "sender_role": "bot",
            "message_type": tipo,
            "conversation_id": conversation_id,
            "created_at": created_at
        }, on_conflict=["message_hash"]).execute()

    except Exception as e:
        logging.error(f"❌ Error registrando interacción: {e}")


def guardar_mensaje_usuario(conversation_id, mensaje, created_at=None):
    if not conversation_id or not mensaje:
        return None

    created_at = created_at or datetime.now(timezone.utc).replace(second=0, microsecond=0)
    message_hash = hashlib.sha256(f"{conversation_id}-{mensaje}-{created_at.isoformat()}-user".encode()).hexdigest()

    data = {
        "conversation_id": conversation_id,
        "sender_role": "user",
        "message_type": "text",
        "start_time": created_at.isoformat(),
        "user_message": mensaje,
        "message_hash": message_hash
    }

    try:
        resultado = supabase.from_("interaction_history").upsert(data, on_conflict=["message_hash"]).execute()
        return resultado.data[0]["id"] if resultado.data else None
    except Exception as e:
        logging.error(f"❌ Error guardando mensaje del usuario: {e}")
        return None




# === Webhook ===
@app.route("/actualizar_estado", methods=["POST", "OPTIONS"])
def actualizar_estado():
    if request.method == "OPTIONS":
        return '', 204  # Respuesta vacía válida para CORS preflight

    data = request.json
    reserva_id = data.get("reserva_id")
    nuevo_estado = data.get("estado")

    if not reserva_id or not nuevo_estado:
        return {"error": "Faltan datos"}, 400

    try:
        # 1️⃣ Obtener user_id y verificar si ya se envió encuesta
        resultado = supabase.from_("reservaciones")\
            .select("user_id, encuesta_enviada")\
            .eq("id", reserva_id).limit(1).execute()

        if not resultado.data:
            return {"error": "Reserva no encontrada"}, 404

        user_id = resultado.data[0]["user_id"]
        ya_enviada = resultado.data[0].get("encuesta_enviada", False)

        # 2️⃣ Actualizar el estado de la reserva
        supabase.from_("reservaciones").update({
            "estado": nuevo_estado
        }).eq("id", reserva_id).execute()

        # 3️⃣ Si es completada y no se ha enviado encuesta → enviar
        if nuevo_estado == "completada" and not ya_enviada:
            config = supabase.from_("bot_configuracion")\
                .select("negocio")\
                .order("created_at", desc=True)\
                .limit(1).execute()
            nombre_negocio = config.data[0]["negocio"] if config.data else "nuestro servicio"

            enviar_encuesta_satisfaccion(user_id, reserva_id, nombre_negocio)

        return {"ok": True}, 200

    except Exception as e:
        logging.error(f"❌ Error actualizando estado: {e}")
        return {"error": "Error interno"}, 500


def limpiar_estados_antiguos():
    while True:
        try:
            ahora = datetime.now(timezone.utc)
            resultado = supabase.from_("reserva_pendiente").select("user_id, created_at").execute()
            for r in resultado.data or []:
                creado = parser.isoparse(r["created_at"])
                if ahora - creado > timedelta(minutes=30):
                    eliminar_estado_pendiente(r["user_id"])
                    responder_y_registrar(r["user_id"], "⚠️ Tu reserva fue cancelada automáticamente por inactividad.")
        except Exception as e:
            logging.error(f"❌ Error limpiando reservas pendientes: {e}")
        time.sleep(600)  # Cada 10 minutos

def iniciar_citas_bot():
    import threading
    logging.info("🚀 Iniciando hilos de citas_bot...")

    try:
        threading.Thread(target=enviar_recordatorios, daemon=True).start()
        threading.Thread(target=limpiar_estados_antiguos, daemon=True).start()
        threading.Thread(target=cron_encuesta_satisfaccion, daemon=True).start()
        logging.info("✅ Hilos de citas_bot iniciados correctamente.")
    except Exception as e:
        logging.error(f"❌ Error al iniciar hilos de citas_bot: {e}")



if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)