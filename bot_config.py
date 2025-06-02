import os

# ✅ Funciones mínimas que todos los bots deben tener disponibles
FUNCIONES_COMUNES = [
    "saludo_inicial",
    "ayuda",
    "reiniciar_conversacion"
]

BOT_PLANTILLAS = {
    "ventas": {
        "nombre": "🤝 Bot de Ventas",
        "descripcion": "Captura y califica leads, da seguimiento y analiza conversaciones para cerrar ventas.",
        "funciones": [
            "identificar_leads",
            "calificar_leads",
            "dar_seguimiento",
            "analizar_archivos",
            "generar_analisis_conversacion"
        ] + FUNCIONES_COMUNES,
        "usa_embeddings": True
    },

    "reservas": {
        "nombre": "📆 Bot de Reservas",
        "descripcion": "Gestiona citas, envía recordatorios, permite cancelaciones y seguimiento post-servicio.",
        "funciones": [
            "agendar_reserva",
            "enviar_recordatorios",
            "cancelar_reserva",
            "post_servicio",
            "analizar_archivos"
        ] + FUNCIONES_COMUNES,
        "usa_embeddings": True
    },

    "asistente": {
        "nombre": "🗂️ Asistente Administrativo",
        "descripcion": "Organiza tareas, recordatorios y notas. Útil para equipos o freelancers.",
        "funciones": [
            "guardar_nota",
            "crear_recordatorio",
            "gestionar_tareas",
            "generar_formato_archivo",
            "analizar_archivos"
        ] + FUNCIONES_COMUNES,
        "usa_embeddings": True
    },

    "entrenado": {
        "nombre": "🧠 Bot de Conocimiento",
        "descripcion": "Responde preguntas sobre productos o servicios usando documentos entrenados.",
        "funciones": [
            "analizar_archivos",
            "responder_por_embeddings",
            "resumir_documentos",
            "generar_faq"
        ] + FUNCIONES_COMUNES,
        "usa_embeddings": True
    },

    "dianx": {
        "nombre": "🧬 Dianx (Asistente Artístico)",
        "descripcion": "Explora temas de arte, lenguaje, cuerpo y poética con sensibilidad crítica.",
        "funciones": [
            "responder_por_embeddings",
            "referenciar_arte",
            "reflexionar_con_usuario",
            "analizar_archivos"
        ] + FUNCIONES_COMUNES,
        "usa_embeddings": True
    },

    "generico": {
    "nombre": "🤖 AIDANA (Asistente Universal)",
    "descripcion": "Responde dudas generales de forma amable y profesional, aunque no tenga contexto del negocio.",
    "funciones": FUNCIONES_COMUNES,
    "usa_embeddings": True,
    "prompt": (
        "Eres AIDANA, un asistente virtual profesional diseñado para ayudar a personas con cualquier tipo de duda general. "
        "Tu personalidad es amigable, clara y confiable. "
        "Preséntate como AIDANA cada vez que el usuario te pregunte tu nombre. "
        "Tienes experiencia ayudando a usuarios en distintos rubros, desde atención al cliente hasta apoyo en tareas administrativas, información general y consejos prácticos. "
        "Contesta siempre de manera amable, breve y clara, aunque no tengas información específica del negocio. "
        "Si no tienes datos concretos, puedes ofrecer ayuda general o canalizar la consulta a un humano si es necesario. "
        "Nunca inventes datos técnicos. Si te preguntan tu nombre, responde: 'Soy AIDANA, tu asistente virtual.' "
        "Si te preguntan para qué sirves, di: 'Estoy aquí para orientarte y ayudarte en lo que pueda, siempre que esté a mi alcance.'"
    )
},


}

# ✅ Devuelve la plantilla correspondiente o la de 'asistente' por defecto
def obtener_plantilla(bot_type: str):
    return BOT_PLANTILLAS.get(bot_type, BOT_PLANTILLAS["asistente"])

# ✅ Devuelve solo las funciones de una plantilla
def obtener_funciones_disponibles(bot_type: str) -> list[str]:
    plantilla = obtener_plantilla(bot_type)
    return plantilla.get("funciones", [])

# ✅ Verifica que las funciones listadas estén implementadas
def validar_plantillas():
    funciones_existentes = {
        "identificar_leads", "calificar_leads", "dar_seguimiento",
        "analizar_archivos", "generar_analisis_conversacion",
        "agendar_reserva", "enviar_recordatorios", "cancelar_reserva", "post_servicio",
        "guardar_nota", "crear_recordatorio", "gestionar_tareas",
        "generar_formato_archivo", "responder_por_embeddings",
        "resumir_documentos", "generar_faq", "referenciar_arte",
        "reflexionar_con_usuario", "saludo_inicial", "ayuda",
        "reiniciar_conversacion"
    }

    for nombre, plantilla in BOT_PLANTILLAS.items():
        for funcion in plantilla["funciones"]:
            if funcion not in funciones_existentes:
                print(f"⚠️ La función '{funcion}' declarada en '{nombre}' no está implementada.")

# ✅ Validación automática solo en desarrollo
if os.getenv("ENV") == "development":
    print("🧪 Validando integridad de plantillas...")
    validar_plantillas()
