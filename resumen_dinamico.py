import logging
from datetime import datetime
from supabase import create_client
from openai import OpenAI

# Asume que estas variables están configuradas por fuera
supabase: create_client
client: OpenAI

def guardar_resumen_usuario(user_id, conversation_id=None):
    try:
        logging.info(f"🔍 Generando resumen actualizado para {user_id}...")

        # Buscar todas las conversaciones del usuario
        conversaciones_res = supabase.from_("conversation_history")\
            .select("conversation_id")\
            .eq("user_id", user_id)\
            .execute()

        conversaciones = conversaciones_res.data or []
        if not conversaciones:
            logging.warning(f"⚠️ No hay conversaciones para el usuario {user_id}")
            return

        conversation_ids = [c["conversation_id"] for c in conversaciones]

        # Extraer historial completo
        interacciones_res = supabase.from_("interaction_history")\
            .select("sender_role, user_message, bot_response, start_time")\
            .in_("conversation_id", conversation_ids)\
            .order("start_time", asc=True)\
            .limit(200)\
            .execute()

        interacciones = interacciones_res.data or []
        if not interacciones:
            logging.warning("⚠️ No hay interacciones suficientes para resumir.")
            return

        historial = []
        for i in interacciones:
            if i["sender_role"] == "user" and i.get("user_message"):
                historial.append(f"Usuario: {i['user_message']}")
            elif i["sender_role"] == "bot" and i.get("bot_response"):
                historial.append(f"Bot: {i['bot_response']}")

        texto_conversacion = "\n".join(historial[-30:])  # Últimos 30 turnos

        # 🔁 Prompt mejorado con objetivos estratégicos
        prompt = """
Actúa como un CRM inteligente. Resume de forma clara:

1. Intención actual del usuario (ej. interesado, cotizando, con dudas, enfriando, cerrado)
2. Etapa del proceso (inicio, medio, cierre, postventa)
3. Estilo y comportamiento (rápido, detallista, informal, técnico)
4. Qué se ha hablado y qué falta resolver
5. Siguiente mejor acción a tomar

Redacta un resumen útil para que la IA continúe la conversación con contexto.
"""

        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": texto_conversacion}
            ],
            temperature=0.5,
            max_tokens=300
        )
        resumen = response.choices[0].message.content.strip()
        logging.info(f"📝 Resumen generado:\n{resumen}")

        # Embedding del resumen
        embedding_response = client.embeddings.create(
            model="text-embedding-ada-002",
            input=resumen
        )
        vector = embedding_response.data[0].embedding

        # Guardar resumen y vector
        supabase.from_("user_summary_embeddings").upsert({
            "user_id": user_id,
            "conversation_id": conversation_id,
            "resumen_texto": resumen,
            "embedding_vector": vector,
            "updated_at": datetime.utcnow().isoformat()
        }, on_conflict=["user_id"]).execute()

        logging.info(f"🧠 Resumen actualizado guardado para {user_id}")

    except Exception as e:
        logging.error(f"❌ Error generando resumen actualizado: {e}")


def actualizar_resumen_si_evento(user_id, conversation_id, evento: str):
    eventos_relevantes = [
        "cotizacion_respondida",
        "cita_agendada",
        "usuario_reaparece",
        "preguntas_post_venta",
        "nueva_intencion_detectada"
    ]

    if evento in eventos_relevantes:
        logging.info(f"📌 Evento relevante '{evento}' detectado para {user_id}. Actualizando resumen...")
        guardar_resumen_usuario(user_id, conversation_id)
