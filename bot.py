import os
import re
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from openai import OpenAI
from telegram import Bot

# Cargar variables de entorno
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
JORGE_WHATSAPP = os.getenv("JORGE_WHATSAPP")
JORGE_CHAT_ID = int(os.getenv("JORGE_CHAT_ID", "6788836691"))
AUTORIZADO = "whatsapp:+5212212411481"

# Inicializar clientes
client = OpenAI(api_key=OPENAI_API_KEY)
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)

# Setup
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
conversations = {}
esperando_nss = {}
estado_usuario = {}
ultimo_mensaje = {}

mensaje_conciencia = (
    "\U0001f4cc *Informaci\u00f3n importante si cotizas bajo la Ley 97:*\n\n"
    "\U0001f538 Si no usas tu ahorro o no ejerces alg\u00fan cr\u00e9dito, ese dinero ser\u00e1 utilizado autom\u00e1ticamente para pagar tu pensi\u00f3n.\n"
    "*Es decir, \u00a1NO LO COBRAR\u00c1S al final de tu vida laboral!*\n\n"
    "\U0001f4c9 Adem\u00e1s, al estar administrado por INFONAVIT, solo genera un *2% de inter\u00e9s anual*, mientras que la inflaci\u00f3n en M\u00e9xico es *mayor al 5%*.\n"
    "*En t\u00e9rminos reales, est\u00e1s perdiendo valor a\u00f1o con a\u00f1o.*\n\n"
    "Por eso es importante tomar acci\u00f3n ahora. \U0001f4a1"
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
"""

def detectar_nss(texto):
    return re.findall(r'\b\d{11}\b', texto)

def detectar_nombre_y_nss(texto):
    nss = detectar_nss(texto)
    nombre = texto.replace(nss[0], "").strip() if nss else None
    return nombre, nss[0] if nss else None

@app.route("/whatsapp", methods=['POST'])
def webhook():
    sender = request.form.get('From')
    msg = request.form.get('Body').strip()

    if sender != AUTORIZADO:
        logging.warning(f"\u274c N\u00famero no autorizado: {sender}")
        return "N\u00famero no autorizado para pruebas con Sandbox.", 403

    user_id = sender
    logging.info(f"\ud83d\udce9 Mensaje recibido de {sender}: {msg}")

    now = datetime.now()
    if user_id in ultimo_mensaje:
        if now - ultimo_mensaje[user_id] > timedelta(minutes=4):
            conversations[user_id].append({"role": "assistant", "content": "Hola de nuevo \ud83d\udc4b, \u00bfen qu\u00e9 m\u00e1s puedo ayudarte hoy? \ud83d\ude0a"})
    ultimo_mensaje[user_id] = now

    if user_id not in conversations:
        conversations[user_id] = []

    conversations[user_id].append({"role": "user", "content": msg})
    response = MessagingResponse()
    resp_msg = response.message()

    try:
        mensaje_normalizado = msg.lower().replace("\u00e1", "a").replace("\u00e9", "e").replace("\u00ed", "i").replace("\u00f3", "o").replace("\u00fa", "u")
        keywords = ["donde estan", "donde se ubican", "ubicacion", "direccion", "direccion exacta", "domicilio", "visitar", "oficina", "como llegar", "en donde estan", "estan ubicados", "me puedes dar la direccion", "mapa", "telefono", "agendar cita", "tienen local", "atienden fisicamente", "estan en cdmx", "son presenciales", "puedo ir", "donde se encuentran", "en donde est\u00e1n", "puedo pedir mas informacion a algun numero", "numero de contacto", "como contactarlos"]

        if any(kw in mensaje_normalizado for kw in keywords):
            respuesta_ubicacion = (
                "\ud83d\udccd Estamos ubicados en *Badianes 103, Residencial Jardines, Lerdo, Durango.*\n\n"
                "\ud83d\udcde Puedes llamarnos al *871 457 2902* para agendar una cita o resolver tus dudas.\n\n"
                "\ud83d\udddf\ufe0f Tambi\u00e9n puedes vernos en Google Maps:\n"
                "https://www.google.com/maps/place/Badianes+103,+Lerdo,+Dgo.\n\n"
                "Ser\u00e1 un gusto atenderte personalmente."
            )
            resp_msg.body(respuesta_ubicacion)
            return str(response)

        if "ya cotizo" in mensaje_normalizado or "si cotizo" in mensaje_normalizado:
            estado_usuario[user_id] = "cotiza"
            conversations[user_id].append({"role": "user", "content": "Cambio de estado: el usuario ahora s\u00ed cotiza"})

        if esperando_nss.get(user_id):
            nombre, nss = detectar_nombre_y_nss(msg)
            if nss:
                esperando_nss[user_id] = False
                fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
                mensaje_confirm = (
                    f"\u00a1Excelente decisi\u00f3n! Ya con esta informaci\u00f3n, uno de nuestros asesores se pondr\u00e1 en contacto.\n"
                    f"Por ahora no necesitamos m\u00e1s documentos. \u00a1Gracias por su confianza!\n\n"
                    f"\u00bfTiene alguna otra pregunta o inquietud que pueda atender en este momento?"
                )
                notificacion = (
                    f"\ud83d\udc4b Hola Jorge,\nNuevo interesado desde WhatsApp:\n\n"
                    f"\ud83d\udccc Nombre: {nombre.title() if nombre else 'Desconocido'}\n"
                    f"\ud83d\uddd3 NSS: {nss}\n"
                    f"\ud83d\udcde WhatsApp: {sender}\n"
                    f"\u23f0 Fecha: {fecha}"
                )
                twilio_client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=JORGE_WHATSAPP, body=notificacion)
                telegram_bot.send_message(chat_id=JORGE_CHAT_ID, text=notificacion)
                resp_msg.body(mensaje_confirm)
                return str(response)
            else:
                resp_msg.body(
                    "Gracias por compartirlo \ud83d\ude4c, pero creo que el n\u00famero no est\u00e1 completo.\n\n"
                    "\u2728 El NSS debe tener *exactamente 11 d\u00edgitos*. A veces se nos puede ir un n\u00famero o un espacio de m\u00e1s \ud83d\ude09.\n\n"
                    "\u00bfPodr\u00edas revisarlo y volver a enviarlo por favor?"
                )
                return str(response)

        messages = [{"role": "system", "content": CONTEXT}] + conversations[user_id][-5:]
        gpt_response = client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            temperature=0.6,
            max_tokens=500
        )
        bot_reply = gpt_response.choices[0].message.content.strip()

        if any(frase in bot_reply.lower() for frase in ["puede aplicar a", "puede recuperar", "requiere tener m\u00e1s de 46 a\u00f1os"]):
            esperando_nss[user_id] = True
            bot_reply += f"\n\n{mensaje_conciencia}\n\n\ud83d\udc49 Por favor, proporcione su N\u00famero de Seguro Social (NSS)."

        conversations[user_id].append({"role": "assistant", "content": bot_reply})
        resp_msg.body(bot_reply)
        return str(response)

    except Exception as e:
        logging.error(f"\u274c Error procesando mensaje: {e}")
        resp_msg.body("Lo siento, ocurri\u00f3 un error. Int\u00e9ntelo de nuevo.")
        return str(response)

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)

