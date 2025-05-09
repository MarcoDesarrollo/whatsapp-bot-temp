import os
import re
import logging
import unicodedata
import requests
from datetime import datetime, timezone
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, request
from openai import OpenAI
from telegram import Bot
from supabase import create_client, Client
from twilio.rest import Client as TwilioClient

# Cargar variables de entorno
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
JORGE_WHATSAPP = os.getenv("JORGE_WHATSAPP")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
FB_VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN")
twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH)

# Inicializar servicios
client = OpenAI(api_key=OPENAI_API_KEY)
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Estados
conversations = {}
esperando_nss = {}
estado_usuario = {}
ultimo_mensaje = {}

mensaje_conciencia = (
    "📌 *Información importante si cotizas bajo la Ley 97:*\n\n"
    "🔸 Si no usas tu ahorro tu dinero será utilizado automáticamente para pagar tu pensión."
    "*¡NO LO COBRARÁS al final de tu vida laboral!*\n\n"
    "Tu dinero al estar administrado por INFONAVIT, sólo genera un *2% de interés anual*😞👎🏻, *mientras que la inflación en México es mayor al 5%*.\n"
    "Por eso es importante tomar acción ahora. 💡"
)

CONTEXT = """
### IDENTIDAD DEL ASISTENTE
Eres Sofía \ud83e\udd5a, asistente virtual de una consultoría llamada *Gestoría Calificada*, especializada en pensiones, INFONAVIT y seguridad social en México. Estoy aquí para orientarte con precisión y rapidez. 🤓

### INTRODUCCIÓN AMABLE (SIEMPRE DEBE SALUDAR)
Al comenzar cualquier conversación, usa esta frase (o una versión natural):

"Hola 👋, soy Sofía. Bienvenido a *Gestoría Calificada*. Soy tu asesora digital especializada en pensiones e INFONAVIT. ¿En qué aspecto puedo ayudarte hoy?"

Después del saludo, inicia la precalificación sin rodeos.

---

### FLUJO DE CLASIFICACIÓN AUTOMÁTICA
Tu objetivo es precalificar al usuario en máximo dos mensajes. Sé clara, directa y no divagues. 

---

🔷 **1. Si el usuario cotiza actualmente al IMSS**:
⚠️ No pidas ningún documento.
Simplemente menciona lo siguiente:

> "Perfecto. Si usted cotiza actualmente, puede aplicar a dos opciones:
> • *Inversión a 12 meses*: recuperar hasta el *95% del ahorro INFONAVIT*  
> • *Inversión a 6 meses*: recuperar hasta el *65%*
> Ambas aplican si tiene más de *$150,000* en INFONAVIT.  
> ¿Desea que iniciemos el proceso?"

---

🕘 **2. Si el usuario NO cotiza actualmente al IMSS**:
Haz estas tres preguntas:

- ¿Tiene más de *46 años*?
- ¿Tiene al menos *$100,000* de ahorro en INFONAVIT?
- ¿Cuenta con su estado de cuenta AFORE o comprobante?

🔸 Si responde *SÍ*:
> "Puede aplicar a *INFONAVIT EXPRESS*: recuperar hasta el *60% en 4 a 6 meses*.  
> Requiere tener más de *46 años*, al menos *$100,000* en INFONAVIT y el estado de cuenta de AFORE o comprobante de saldo.  
> ¿Tiene alguno de estos documentos o acceso a *Mi cuenta Infonavit*?"

🔸 Si responde *NO*:
> "Entiendo. Para continuar con el trámite es necesario contar con alguno de los siguientes:
> • Estado de cuenta AFORE  
> • Acceso a 'Mi cuenta Infonavit'  
> • Comprobante de saldo del ahorro Infonavit  
> Te sugerimos obtener uno de ellos y cuando lo tengas, estaremos encantados de ayudarte. 😊"

---

🟡 **3. Si el usuario no sabe si cotiza actualmente**:
Pide su NSS con tono cálido:

> "Para verificar tu situación, ¿podrías compartirme tu *Número de Seguro Social (NSS)*?"

---

### RESPUESTA CUANDO COMPARTA NOMBRE Y NSS:
> "Gracias 🙌. Ya con tu NSS registrado, uno de nuestros asesores te contactará para continuar con el proceso.  
> Por ahora no necesitamos más documentos. ¡Gracias por tu confianza!"

---

### BLOQUE DE EDUCACIÓN FINANCIERA (LEY 97)

💡 *Importante si cotizas bajo la Ley 97* (a partir del *1° de julio de 1997*):

🔸 Si no usas tu ahorro ni ejerces algún crédito, ese dinero será usado automáticamente para pagar tu pensión.  
🔸 *Tú no lo vas a cobrar al final de tu vida laboral*.

📉 Además, como está administrado por INFONAVIT, solo genera un *2% de interés anual*, pero la inflación en México es mayor al *5%*.

👉 En términos reales *estás perdiendo dinero*.

Por eso es tan importante informarse y tomar decisiones a tiempo.

---

### REGLAS DE CONVERSACIÓN

- Siempre responde con cordialidad, cercanía y profesionalismo.
- Usa lenguaje humano, evita sonar como robot.
- No repitas, no recites plantillas.
- No pidas documentos si el usuario *sí cotiza actualmente al IMSS*.
- Valida todo paso a paso. Prioriza ayudar.
- Cierra con preguntas como:
  - "¿Deseas iniciar?"
  - "¿Te gustaría que te ayudemos a comenzar?"
"""  

def detectar_nss(texto):
    return re.findall(r'\b\d{11}\b', texto)

def detectar_nombre_y_nss(texto):
    nss = detectar_nss(texto)
    nombre = texto.replace(nss[0], "").strip() if nss else None
    return nombre, nss[0] if nss else None

def guardar_conversacion_si_no_existe(user_id, numero):
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
                "is_human": False
            }).execute()
            logging.info(f"🆕 Conversación creada: {nueva}")
            return nueva.data[0]["conversation_id"] if nueva.data else None
    except Exception as e:
        logging.error(f"❌ Error accediendo a Supabase: {e}")
        return None


def guardar_mensaje_conversacion(conversation_id, mensaje, sender="user", tipo="text"):
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

    try:
        nuevo = supabase.from_("interaction_history").insert(insert_data).execute()
        message_id = nuevo.data[0]["id"] if nuevo.data else None
        logging.info(f"💬 Mensaje guardado con ID: {message_id}")
        return message_id
    except Exception as e:
        logging.error(f"❌ Error guardando mensaje en interaction_history: {e}")
        return None






def notificar_a_jorge(nombre, nss, numero, fecha):
    mensaje = (
        f"👋 Hola Jorge,\nNuevo interesado desde WhatsApp:\n\n"
        f"📌 Nombre: {nombre}\n"
        f"📅 NSS: {nss}\n"
        f"📱 WhatsApp: {numero}\n"
        f"⏰ Fecha: {fecha}"
    )
    try:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=JORGE_WHATSAPP,
            body=mensaje
        )
        logging.info("✅ Notificación enviada a Jorge")
    except Exception as e:
        logging.error(f"❌ Error notificando a Jorge: {e}")




def detectar_cierre_conversacion(texto):
    frases_cierre = [
        "eso es todo", "sería todo", "muchas gracias", "nada más", 
        "hasta luego", "adiós", "nos vemos", "gracias por ahora", "estamos en contacto"
    ]
    texto_lower = texto.lower()
    return any(frase in texto_lower for frase in frases_cierre)


def generar_analisis_incremental(conversacion):
    try:
        full = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in conversacion])
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": """
                    Eres un analista de marketing digital. Resume en 1 frase el interés del usuario o su intención de compra.
                    No repitas, no seas genérico. Ejemplo: "Usuario interesado en recuperar ahorro Infonavit con Ley 97."
                """},
                {"role": "user", "content": full}
            ],
            max_tokens=50,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Error generando análisis incremental: {e}")
        return None


def generar_analisis_final_openai(conversacion):
    try:
        full = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in conversacion])
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": """
                    Eres un analista de marketing digital. Resume esta conversación en no más de 300 palabras incluyendo:
                    1. Intereses del usuario
                    2. Reclamos o dudas frecuentes
                    3. Posible intención de compra
                    4. Observaciones útiles para ventas
                    5. Tono del mensaje (positivo, negativo, neutral)
                """},
                {"role": "user", "content": full}
            ],
            max_tokens=400,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Error generando análisis final: {e}")
        return None
    
def clasificar_lead(conversacion):
    try:
        texto = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in conversacion])
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": CONTEXT + """

Ahora, clasifica este usuario como: Calificado, Medio o No calificado.

- Calificado: Si dice que sí cotiza al IMSS o no cotiza pero cumple con los requisitos (más de 46 años, ahorro mayor a $100,000 y cuenta con algún documento o acceso a Mi Cuenta Infonavit). También aplica si ya proporcionó su NSS.

- Medio: Si muestra interés pero falta al menos un requisito (por ejemplo: tiene ahorro pero no tiene documento, o tiene menos de 46 años, o no dio su NSS todavía).

- No calificado: Si no tiene ahorro, no tiene documentos, no cumple con los requisitos mínimos, o no responde claramente.

Responde solo con una palabra: Calificado, Medio o No calificado.
"""},
                {"role": "user", "content": texto}
            ],
            max_tokens=10,
            temperature=0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Error clasificando lead: {e}")
        return "No calificado"


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
        nuevo_score = clasificar_lead(conversacion)

        if nuevo_score and nuevo_score.lower() != (score_actual or "").lower():
            actualizar_lead_score(conversation_id, nuevo_score.lower())
            logging.info(f"🔄 Lead_score actualizado: {score_actual} → {nuevo_score}")

            # Guardar en historial
            try:
                supabase.from_("lead_score_history").insert({
                    "conversation_id": conversation_id,
                    "old_score": score_actual,
                    "new_score": nuevo_score.lower(),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }).execute()
                logging.info("📈 Historial de lead_score guardado correctamente")
            except Exception as e:
                logging.warning(f"⚠️ No se pudo guardar historial de score: {e}")

            # Asegurar que el lead también exista en conversation_history
            try:
                existente = supabase.from_("conversation_history")\
                    .select("conversation_id")\
                    .eq("conversation_id", conversation_id)\
                    .limit(1)\
                    .execute()
                
                if not existente.data:
                    supabase.from_("conversation_history").insert({
                        "conversation_id": conversation_id,
                        "lead_score": nuevo_score.lower(),
                        "channel": "whatsapp",
                        "user_id": user_id,
                        "user_identifier": user_id,
                        "status": "bot",
                        "is_human": False,
                        "created_at": datetime.now(timezone.utc).isoformat()
                    }).execute()
                    logging.info("📝 Lead no calificado registrado también en conversation_history")
            except Exception as e:
                logging.warning(f"⚠️ No se pudo insertar en conversation_history: {e}")

    except Exception as e:
        logging.error(f"❌ Error reevaluando lead_score dinámicamente: {e}")


def enviar_bienvenida_con_imagen(sender, conversation_id):
    try:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{sender}",
            body="Hola 👋, soy Sofía. Bienvenido a *Gestoría Calificada*. Soy tu asesora digital especializada en pensiones e INFONAVIT. ¿En qué aspecto puedo ayudarte hoy?",
           media_url=["https://i.imgur.com/HeS6Tcr.jpeg"]
        )
        guardar_mensaje_conversacion(
            conversation_id,
            "Mensaje de bienvenida + imagen enviada",
            sender="bot",
            tipo="image"
        )
        logging.info("✅ Imagen de bienvenida enviada correctamente")
    except Exception as e:
        logging.error(f"❌ Error enviando imagen de bienvenida: {e}")

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


def handle_messenger_message(sender_id, text):
    user_id = f"messenger_{sender_id}"  # prefijo para diferenciar canal
    channel = "messenger"

    # Guardar conversación si no existe
    conversation_id = guardar_conversacion_messenger_si_no_existe(user_id, sender_id)

    if conversation_id:
        guardar_mensaje_conversacion(conversation_id, text, sender="user", tipo="text")

    if user_id not in conversations:
        conversations[user_id] = []
    conversations[user_id].append({"role": "user", "content": text})

    messages = [{"role": "system", "content": CONTEXT}] + conversations[user_id][-5:]
    response = client.chat.completions.create(
        model="gpt-4",
        messages=messages,
        temperature=0.6,
        max_tokens=500
    )
    bot_reply = response.choices[0].message.content.strip()

    conversations[user_id].append({"role": "assistant", "content": bot_reply})
    guardar_mensaje_conversacion(conversation_id, bot_reply, sender="bot", tipo="text")

    send_messenger_message(sender_id, bot_reply)

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


@app.route("/whatsapp", methods=['POST'])
def webhook():
    if not request.form:
        logging.warning("⚠️ Payload vacío o no enviado como form-data")
        return "Se esperaba form-data (Twilio)", 400

    try:
        msg = request.form.get("Body", "").strip()
        sender = request.form.get("From", "").replace("whatsapp:", "")
        user_id = sender
        logging.info(f"📩 Mensaje recibido de {sender}: {msg}")

        now = datetime.now()
        if user_id in ultimo_mensaje:
            if now - ultimo_mensaje[user_id] > timedelta(minutes=4):
                conversations[user_id].append({
                    "role": "assistant",
                    "content": "Hola de nuevo 👋, ¿en qué más puedo ayudarte hoy? 😊"
                })
        ultimo_mensaje[user_id] = now

        if user_id not in conversations:
            conversations[user_id] = []
        conversations[user_id].append({"role": "user", "content": msg})

        # ✅ Primero obtenemos conversation_id
        conversation_id = guardar_conversacion_si_no_existe(user_id, sender)

        if len(conversations[user_id]) == 1:
           enviar_bienvenida_con_imagen(sender, conversation_id)
        
        intervencion = supabase.from_("conversation_history")\
          .select("is_human")\
          .eq("conversation_id", conversation_id)\
          .limit(1)\
          .execute()
        
        if intervencion.data and intervencion.data[0]["is_human"]:
            logging.info(f"🤖 Intervención humana activa. Bot no responderá (user: {user_id})")
            guardar_mensaje_conversacion(conversation_id, msg, sender="user", tipo="text")
            return "ok (intervención activa)", 200
        
        # ✅ Luego guardamos el mensaje del usuario
        message_id = guardar_mensaje_conversacion(conversation_id, msg, sender="user", tipo="text")

        # ✅ Verificar si hay archivo adjunto en el mensaje
        num_media = int(request.form.get("NumMedia", 0))
        msg = request.form.get("Body", "").strip()
        sender = request.form.get("From", "").replace("whatsapp:", "")
        user_id = sender
        logging.info(f"📩 Mensaje recibido de {sender}: {msg or '[archivo adjunto sin texto]'}")

        # ⏳ Guardar mensaje de texto si existe
        if msg and not num_media:
            guardar_mensaje_conversacion(conversation_id, msg, sender="user", tipo="text")

        if num_media > 0:
            for i in range(num_media):
                media_url = request.form.get("MediaUrl0")
                media_type = request.form.get("MediaContentType0")

            # Clasificar tipo de mensaje según el mime-type
                if media_type.startswith("image/"):
                    tipo_archivo = "image"
                elif media_type.startswith("video/"):
                    tipo_archivo = "video"
                elif media_type == "application/pdf":
                    tipo_archivo = "file"
                else:
                     tipo_archivo = "file"

                try:
                    supabase.from_("interaction_history").insert({
                        "conversation_id": conversation_id,
                        "sender_role": "user",
                        "message_type": tipo_archivo,
                        "file_url": media_url,
                        "file_type": media_type,
                        "start_time": datetime.now(timezone.utc).isoformat()
                    }).execute()

                    logging.info(f"📎 Archivo recibido ({media_type}): {media_url}")
                except Exception as e:
                    logging.error(f"❌ Error guardando archivo en interaction_history: {e}")


                                 
        reevaluar_lead_score_dinamico(conversation_id, conversations[user_id], user_id)

        if message_id:
            try:
                single_analysis = generar_analisis_incremental([{"role": "user", "content": msg}])
                if single_analysis:
                    supabase.from_("interaction_history").update({
                         "analysis_result": single_analysis
                    }).eq("id", message_id).execute()
            except Exception as e:
                logging.error(f"❌ Error guardando análisis individual en tiempo real: {e}")

        mensaje_normalizado = ''.join(
            c for c in unicodedata.normalize('NFD', msg.lower())
            if unicodedata.category(c) != 'Mn'
        )

        keywords = [
            "donde estan", "donde se ubican", "ubicacion", "direccion", "direccion exacta",
            "domicilio", "visitar", "oficina", "como llegar", "en donde estan", "estan ubicados",
            "me puedes dar la direccion", "mapa", "telefono", "agendar cita", "tienen local",
            "atienden fisicamente", "estan en cdmx", "son presenciales", "puedo ir",
            "donde se encuentran", "en donde se ubican"
        ]

        if any(kw in mensaje_normalizado for kw in keywords):
            mensaje_texto = (
                "📍 Estamos ubicados en *Badianes 103, Residencial Jardines, Lerdo, Durango.*\n\n"
                "📞 Puedes llamarnos al *871 457 2902* para agendar una cita o resolver tus dudas.\n\n"
                "🗺️ También puedes vernos en Google Maps:\n"
                "https://www.google.com/maps/place/Badianes+103,+Lerdo,+Dgo.\n\n"
                "Será un gusto atenderte personalmente."
            )
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=f"whatsapp:{sender}",
                body=mensaje_texto
            )
            return "ok", 200
        
        if "formato" in mensaje_normalizado or "plantilla" in mensaje_normalizado:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=f"whatsapp:{sender}",
                body="Aquí está la plantilla que necesitas:",
                media_url=["https://midominio.com/docs/plantilla-solicitud.pdf"]
            )
            return "ok", 200

        if "ya cotizo" in mensaje_normalizado or "si cotizo" in mensaje_normalizado:
            estado_usuario[user_id] = "cotiza"
            conversations[user_id].append({"role": "user", "content": "Cambio de estado: el usuario ahora sí cotiza"})
            actualizar_etapa_conversacion(conversation_id, "cotiza")


        if esperando_nss.get(user_id):
            nombre, nss = detectar_nombre_y_nss(msg)
            if nss:
                esperando_nss[user_id] = False
                fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
                guardar_mensaje_conversacion(conversation_id, f"NSS recibido: {nss}", sender="user", tipo="nss")

                mensaje_confirm = (
                    "¡Excelente decisión! Ya con esta información, uno de nuestros asesores se pondrá en contacto.\n"
                    "Por ahora no necesitamos más documentos. ¡Gracias por su confianza!\n\n"
                    "¿Tiene alguna otra pregunta o inquietud que pueda atender en este momento?"
                )
                notificacion = (
                    f"👋 Hola Jorge,\nNuevo interesado desde WhatsApp:\n\n"
                    f"📌 Nombre: {nombre.title() if nombre else 'Desconocido'}\n"
                    f"📅 NSS: {nss}\n"
                    f"📱 WhatsApp: {sender}\n"
                    f"⏰ Fecha: {fecha}"
                )

                twilio_client.messages.create(
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=JORGE_WHATSAPP,
                    body=notificacion
                )

                twilio_client.messages.create(
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=f"whatsapp:{sender}",
                    body=mensaje_confirm
                )

                supabase.from_("conversation_history").update({
                    "has_nss": True,
                    "nss": nss,
                    "status": "listo_para_contactar",
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }).eq("conversation_id", conversation_id).execute()

                actualizar_etapa_conversacion(conversation_id, "nss_confirmado")
                actualizar_lead_score(conversation_id, "calificado")

                lead_score = clasificar_lead(conversations[user_id])
                logging.info(f"🏷️ Lead clasificado tras NSS como: {lead_score}")
                actualizar_lead_score(conversation_id, "calificado")

                return "ok", 200
            else:
                error_msg = (
                    "Gracias por compartirlo 🙌, pero creo que el número no está completo.\n\n"
                    "✨ El NSS debe tener *exactamente 11 dígitos*. A veces se nos puede ir un número o un espacio de más 😉.\n\n"
                    "¿Podrías revisarlo y volver a enviarlo por favor?"
                )
                twilio_client.messages.create(
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=f"whatsapp:{sender}",
                    body=error_msg
                )
                return "ok", 200

        messages = [{"role": "system", "content": CONTEXT}] + conversations[user_id][-5:]
        gpt_response = client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            temperature=0.6,
            max_tokens=500
        )
        bot_reply = gpt_response.choices[0].message.content.strip()

        if any(frase in bot_reply.lower() for frase in [
            "puede aplicar a", "puede recuperar", "requiere tener más de 46 años"
        ]):
            esperando_nss[user_id] = True
            bot_reply += f"\n\n{mensaje_conciencia}\n\n👉 Por favor, proporcione su Número de Seguro Social (NSS)."
            actualizar_etapa_conversacion(conversation_id, "esperando_nss")

            actualizar_lead_score(conversation_id, "medio")


        conversations[user_id].append({"role": "assistant", "content": bot_reply})
        guardar_mensaje_conversacion(conversation_id, bot_reply, sender="bot", tipo="text")

        # 👇 Análisis por mensaje en tiempo real
        # try:
             #single_analysis = generar_analisis_incremental([{"role": "user", "content": msg}, {"role": "assistant", "content": bot_reply}])
            # if single_analysis:
                # supabase.from_("interaction_history").insert({
                    # "conversation_id": conversation_id,
                    # "sender_role": "bot",
                    # "message_type": "analysis",
                    # "start_time": datetime.now(timezone.utc).isoformat(),
                    # "analysis_result": single_analysis
                 #}).execute()
        # except Exception as e:
             #logging.error(f"❌ Error guardando análisis individual en tiempo real: {e}")


        if len(conversations[user_id]) % 4 == 0:
            logging.info(f"🧠 Generando análisis corto para {user_id}")
            insight = generar_analisis_incremental(conversations[user_id][-6:])
            if insight:
                supabase.from_("conversation_history").update({
                    "analysis_incremental": insight,
                }).eq("conversation_id", conversation_id).execute()

        if detectar_cierre_conversacion(msg):
            logging.info(f"🛑 Frase de cierre detectada. Generando análisis final para {user_id}")
            analisis = generar_analisis_final_openai(conversations[user_id])
            lead_score = clasificar_lead(conversations[user_id])
            logging.info(f"🏷️ Lead clasificado como: {lead_score}")

            if analisis:
                supabase.from_("conversation_history").update({
                    "final_analysis": analisis,
                    "status": "finalizado"
                }).eq("conversation_id", conversation_id).execute()

                # Usa función centralizada para actualizar lead_score con timestamp
                actualizar_lead_score(conversation_id, lead_score)

            actualizar_etapa_conversacion(conversation_id, "finalizado")
        
                # === 🔁 Reevaluación dinámica de lead_score (última validación antes de responder) ===
        try:
            resultado = supabase.from_("conversation_history")\
                .select("lead_score")\
                .eq("conversation_id", conversation_id)\
                .limit(1)\
                .execute()

            score_actual = resultado.data[0]["lead_score"] if resultado.data else None
            nuevo_score = clasificar_lead(conversations[user_id])

            if nuevo_score and nuevo_score.lower() != (score_actual or "").lower():
                actualizar_lead_score(conversation_id, nuevo_score.lower())
                logging.info(f"🔄 Lead_score actualizado: {score_actual} → {nuevo_score}")

                # (Opcional) Guardar historial de cambios si decides crear esta tabla
                try:
                    supabase.from_("lead_score_history").insert({
                        "conversation_id": conversation_id,
                        "old_score": score_actual,
                        "new_score": nuevo_score.lower(),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }).execute()
                    logging.info("📈 Historial de lead_score guardado correctamente")
                except Exception as e:
                    logging.warning(f"⚠️ No se pudo guardar historial de score: {e}")
        except Exception as e:
            logging.error(f"❌ Error reevaluando lead_score dinámicamente: {e}")
            

        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{sender}",
            body=bot_reply
        )
        return "ok", 200

    except Exception as e:
        logging.error(f"❌ Error procesando mensaje: {e}")
        return "Lo siento, ocurrió un error. Inténtelo de nuevo.", 200
    
@app.route("/messenger", methods=['GET', 'POST'])
def messenger_webhook():
    if request.method == 'GET':
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == FB_VERIFY_TOKEN:
            logging.info("✅ Verificación de webhook Messenger exitosa.")
            return challenge, 200
        else:
            logging.warning(f"❌ Verificación fallida. Mode: {mode}, Token recibido: {token}")
            return "Forbidden", 403

    if request.method == 'POST':
        try:
            data = request.get_json(force=True)
            logging.info("📩 Evento recibido en webhook Messenger:")
            logging.info(data)  # 🔍 Esto imprimirá todo lo que venga del Webhook

            for entry in data.get("entry", []):
                for event in entry.get("messaging", []):
                    sender_id = event["sender"]["id"]
                    message_text = event.get("message", {}).get("text")

                    if message_text:
                        logging.info(f"✉️ Mensaje de {sender_id}: {message_text}")
                        handle_messenger_message(sender_id, message_text)
                    else:
                        logging.info(f"⚠️ Evento sin texto: {event}")
        except Exception as e:
            logging.error(f"❌ Error procesando mensaje de Messenger: {e}")

        return "ok", 200




if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)

