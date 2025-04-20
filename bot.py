import os
import re
import logging
import unicodedata
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, request
from openai import OpenAI
from telegram import Bot

# Cargar variables de entorno
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WHATSAPP_CLOUD_API_TOKEN = os.getenv("WHATSAPP_CLOUD_API_TOKEN")
WHATSAPP_CLOUD_PHONE_ID = os.getenv("WHATSAPP_CLOUD_PHONE_ID")
WHATSAPP_TEMPLATE_NAME = os.getenv("WHATSAPP_TEMPLATE_NAME")
JORGE_WHATSAPP = os.getenv("JORGE_WHATSAPP")
JORGE_CHAT_ID = int(os.getenv("JORGE_CHAT_ID", "6788836691"))

# Inicializar servicios
client = OpenAI(api_key=OPENAI_API_KEY)
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Estados
conversations = {}
esperando_nss = {}
estado_usuario = {}
ultimo_mensaje = {}

mensaje_conciencia = (
    "📌 *Información importante si cotizas bajo la Ley 97:*\n\n"
    "🔸 Si no usas tu ahorro o no ejerces algún crédito, ese dinero será utilizado automáticamente para pagar tu pensión.\n"
    "*Es decir, ¡NO LO COBRARÁS al final de tu vida laboral!*\n\n"
    "📉 Además, al estar administrado por INFONAVIT, solo genera un *2% de interés anual*, mientras que la inflación en México es *mayor al 5%*.\n"
    "*En términos reales, estás perdiendo valor año con año.*\n\n"
    "Por eso es importante tomar acción ahora. 💡"
)

CONTEXT = """
### IDENTIDAD DEL ASISTENTE
Eres Sofía \ud83e\udd5a, asistente virtual de una consultoría llamada *Gestoría C en pensiones*, especializada en pensiones, INFONAVIT y seguridad social en México. Estoy aquí para orientarte con precisión y rapidez. 🤓

### INTRODUCCIÓN AMABLE (SIEMPRE DEBE SALUDAR)
Al comenzar cualquier conversación, usa esta frase (o una versión natural):

"Hola 👋, soy Sofía. Bienvenido a *Gestoría C en pensiones*. Soy tu asesora digital especializada en pensiones e INFONAVIT. ¿En qué aspecto puedo ayudarte hoy?"

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
"""  # (Aquí dejas todo tu bloque de contexto tal como ya lo tienes) # Tu bloque completo de contexto GPT (como lo pegaste antes)

def detectar_nss(texto):
    return re.findall(r'\b\d{11}\b', texto)

def detectar_nombre_y_nss(texto):
    nss = detectar_nss(texto)
    nombre = texto.replace(nss[0], "").strip() if nss else None
    return nombre, nss[0] if nss else None

def send_whatsapp_template_message(to, nombre, nss, numero, fecha):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_CLOUD_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_CLOUD_API_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to.replace("whatsapp:", ""),
        "type": "template",
        "template": {
            "name": WHATSAPP_TEMPLATE_NAME,
            "language": {"code": "es_MX"},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": nombre or "Desconocido"},
                    {"type": "text", "text": nss},
                    {"type": "text", "text": numero.replace("whatsapp:", "")},
                    {"type": "text", "text": fecha}
                ]
            }]
        }
    }
    response = requests.post(url, headers=headers, json=payload)
    logging.info(f"[Cloud API] Respuesta: {response.status_code} - {response.text}")

@app.route("/whatsapp", methods=["GET"])
def verify_webhook():
    VERIFY_TOKEN = "mi_webhook_token"
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente")
        return challenge, 200
    else:
        logging.warning("❌ Falló la verificación del webhook")
        return "Token de verificación inválido", 403

@app.route("/whatsapp", methods=['POST'])
def webhook():
    data = request.get_json()
    logging.info(f"📥 Payload recibido: {data}")

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        messages = value.get("messages")

        if not messages:
            return "No hay mensajes", 200

        message = messages[0]
        sender = message["from"]
        msg = message["text"]["body"].strip()
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
            try:
                response_api = requests.post(
                    f"https://graph.facebook.com/v19.0/{WHATSAPP_CLOUD_PHONE_ID}/messages",
                    headers={
                        "Authorization": f"Bearer {WHATSAPP_CLOUD_API_TOKEN}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "messaging_product": "whatsapp",
                        "to": sender,
                        "type": "text",
                        "text": {"body": mensaje_texto}
                    }
                )
                logging.info(f"✅ Ubicación enviada por Cloud API: {response_api.status_code} - {response_api.text}")
            except Exception as e:
                logging.warning(f"❌ No se pudo enviar ubicación con Cloud API: {e}")
            return "ok", 200

        if "ya cotizo" in mensaje_normalizado or "si cotizo" in mensaje_normalizado:
            estado_usuario[user_id] = "cotiza"
            conversations[user_id].append({"role": "user", "content": "Cambio de estado: el usuario ahora sí cotiza"})

        if esperando_nss.get(user_id):
            nombre, nss = detectar_nombre_y_nss(msg)
            if nss:
                esperando_nss[user_id] = False
                fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
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
                send_whatsapp_template_message(
                    to=JORGE_WHATSAPP,
                    nombre=nombre.title() if nombre else "Desconocido",
                    nss=nss,
                    numero=sender,
                    fecha=fecha
                )
                telegram_bot.send_message(chat_id=JORGE_CHAT_ID, text=notificacion)

                requests.post(
                    f"https://graph.facebook.com/v19.0/{WHATSAPP_CLOUD_PHONE_ID}/messages",
                    headers={
                        "Authorization": f"Bearer {WHATSAPP_CLOUD_API_TOKEN}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "messaging_product": "whatsapp",
                        "to": sender,
                        "type": "text",
                        "text": {"body": mensaje_confirm}
                    }
                )
                return "ok", 200
            else:
                error_msg = (
                    "Gracias por compartirlo 🙌, pero creo que el número no está completo.\n\n"
                    "✨ El NSS debe tener *exactamente 11 dígitos*. A veces se nos puede ir un número o un espacio de más 😉.\n\n"
                    "¿Podrías revisarlo y volver a enviarlo por favor?"
                )
                requests.post(
                    f"https://graph.facebook.com/v19.0/{WHATSAPP_CLOUD_PHONE_ID}/messages",
                    headers={
                        "Authorization": f"Bearer {WHATSAPP_CLOUD_API_TOKEN}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "messaging_product": "whatsapp",
                        "to": sender,
                        "type": "text",
                        "text": {"body": error_msg}
                    }
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

        conversations[user_id].append({"role": "assistant", "content": bot_reply})

        response_api = requests.post(
            f"https://graph.facebook.com/v19.0/{WHATSAPP_CLOUD_PHONE_ID}/messages",
            headers={
                "Authorization": f"Bearer {WHATSAPP_CLOUD_API_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "messaging_product": "whatsapp",
                "to": sender,
                "type": "text",
                "text": {"body": bot_reply}
            }
        )
        logging.info(f"✅ Respuesta enviada por Cloud API: {response_api.status_code} - {response_api.text}")
        return "ok", 200

    except Exception as e:
        logging.error(f"❌ Error procesando mensaje: {e}")
        return "Lo siento, ocurrió un error. Inténtelo de nuevo.", 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
