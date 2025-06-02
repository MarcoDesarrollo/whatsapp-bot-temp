import logging
from datetime import datetime, timezone
from dateutil import parser
from supabase import create_client
import unicodedata



def actualizar_perfil_comportamiento_usuario(user_id: str, conversation_id: str):
    from bot import supabase as db
    try:
        # 1. Obtener interacciones
        interacciones = db.from_("interaction_history")\
            .select("start_time, user_message")\
            .eq("conversation_id", conversation_id)\
            .order("start_time", desc=False)\
            .execute().data or []

        tiempos, mensajes, prev = [], [], None
        for i in interacciones:
            if prev:
                delta = parser.isoparse(i["start_time"]) - parser.isoparse(prev)
                minutos = delta.total_seconds() / 60
                if 0 < minutos < 180:
                    tiempos.append(minutos)
            prev = i["start_time"]
            if i.get("user_message"):
                mensajes.append(i["user_message"].lower())

        promedio = round(sum(tiempos) / len(tiempos), 2) if tiempos else None

        # 2. Frases clave
        frases_rechazo = [m for m in mensajes if any(x in m for x in ["luego", "revisar", "pensarlo", "pienso"])]
        frases_interes = [m for m in mensajes if any(x in m for x in ["me interesa", "cuánto cuesta", "quiero"])]
        frases_urgencia = [m for m in mensajes if any(x in m for x in ["urge", "lo necesito", "ya"])]

        frases_rechazo = list(set(frases_rechazo))[:5]
        frases_interes = list(set(frases_interes))[:5]
        frases_urgencia = list(set(frases_urgencia))[:5]

        # 3. Estilo
        estilo = "directo" if all(len(m.split()) < 8 for m in mensajes) else "largo"

        # 4. Historial de etapas
        etapas = db.from_("conversation_history")\
            .select("etapa_actual")\
            .eq("user_id", user_id)\
            .execute().data or []
        conteo_etapas = {}
        for e in etapas:
            etapa = e["etapa_actual"]
            conteo_etapas[etapa] = conteo_etapas.get(etapa, 0) + 1

        # 5. Score promedio
        scores = db.from_("conversation_history")\
            .select("lead_score")\
            .eq("user_id", user_id)\
            .execute().data or []

        score_final = None
        if scores:
            cuenta = {"calificado": 0, "medio": 0, "no_calificado": 0}
            for s in scores:
                lead = (s["lead_score"] or "").lower()
                if lead in cuenta:
                    cuenta[lead] += 1
            score_final = max(cuenta, key=cuenta.get)

        # 6. Días activos
        dias = db.from_("conversation_history")\
            .select("updated_at")\
            .eq("user_id", user_id)\
            .execute().data or []

        fechas = sorted([parser.isoparse(f["updated_at"]).date() for f in dias])
        dias_activo = len(set(fechas))
        dias_silencio = (datetime.now(timezone.utc).date() - fechas[-1]).days if fechas else None

        # 7. ¿Responde a seguimiento?
        seguimiento = db.from_("conversation_history")\
            .select("seguimiento_enviado_at, updated_at")\
            .eq("user_id", user_id)\
            .execute().data or []

        responde_a_seguimiento = any(
            parser.isoparse(s["updated_at"]) > parser.isoparse(s["seguimiento_enviado_at"])
            for s in seguimiento if s["seguimiento_enviado_at"]
        ) if seguimiento else False

        # 8. Dominancia de intención (simple por ahora)
        intencion_dominante = "venta" if any("cotiza" in m for m in mensajes) else "informacion"

        # 9. Upsert del perfil
        db.from_("user_behavior_profile").upsert({
            "user_id": user_id,
            "lead_score_promedio": score_final,
            "tiempo_resp_promedio": promedio,
            "frases_rechazo": frases_rechazo,
            "frases_interes": frases_interes,
            "frases_urgencia": frases_urgencia,
            "estilo_mensaje": estilo,
            "historial_etapas": conteo_etapas,
            "dias_activo": dias_activo,
            "dias_silencio": dias_silencio,
            "responde_a_seguimiento": responde_a_seguimiento,
            "intencion_dominante": intencion_dominante,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }, on_conflict=["user_id"]).execute()

        logging.info(f"📊 Perfil comportamiento COMPLETO actualizado para {user_id}")

    except Exception as e:
        logging.error(f"❌ Error generando perfil de comportamiento: {e}")
