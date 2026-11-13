# agent/brain.py — Cerebro del agente: conexion con Claude
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Anthropic.
"""

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

from agent.memory import guardar_conversacion, obtener_ficha  # noqa: E402
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# El modelo se cambia desde .env, sin tocar el codigo.
#   claude-opus-5-5   el mas capaz             $4 / $20 por millon de tokens
#   claude-sonnet-5-5 el balanceado (default)  $2 / $10
#   claude-haiku-5-5  el mas barato y rapido   $0.10 / $0.50
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5-5"

# Es un bot de respuestas cortas: con esfuerzo bajo contesta mas rapido y mas barato.
# Dejalo vacio en el .env para no mandar el parametro.
ESFUERZO = os.getenv("ANTHROPIC_EFFORT", "low").strip()

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: en los modelos
# actuales el razonamiento interno tambien cuenta contra el. Con el margen justo, una
# pregunta que exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "4096")

# Los modelos mas viejos no aceptan output_config. Si la primera llamada falla por eso,
# se reintenta sin el parametro y se recuerda para las siguientes.
_soporta_esfuerzo = True


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """El system prompt: quien es el agente y que sabe del negocio."""
    return cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _extraer_texto(respuesta) -> str:
    """
    Junta el texto de la respuesta de Claude.

    Ojo: NO se puede hacer respuesta.content[0].text. La respuesta es una lista de
    bloques y el primero no siempre es texto (los modelos que razonan devuelven
    primero un bloque de pensamiento). Hay que filtrar por tipo.
    """
    partes = [bloque.text for bloque in respuesta.content if bloque.type == "text"]
    return "\n".join(p for p in partes if p).strip()


def _es_error_de_esfuerzo(error: Exception) -> bool:
    """
    True solo si el modelo rechazo la llamada POR el parametro output_config/effort.

    Se exige que sea un 400 de peticion invalida y no cualquier error que mencione la
    palabra: un 529 de sobrecarga que la nombre de paso no debe apagar el parametro
    para todo el proceso.
    """
    if getattr(error, "status_code", None) != 400:
        return False
    texto = str(error).lower()
    return "output_config" in texto or "effort" in texto


ZONA = ZoneInfo("America/Mexico_City")
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]

# Cuantas vueltas de herramientas se permiten por mensaje del cliente
MAX_RONDAS = 3

HERRAMIENTAS = [
    {
        "name": "registrar_conversacion",
        "description": (
            "Registro interno de la conversacion para el equipo humano de Quetzal. Llamala en "
            "cada turno. Solo deja el caso visible en el panel: NO envia nada a nadie ni "
            "canaliza al cliente, asi que nunca le digas al cliente que ya se transfirio. "
            "Llena solo lo que de verdad sabes; no inventes datos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "categoria": {
                    "type": "string",
                    "enum": [
                        "por_identificar", "cliente_potencial", "cliente_en_seguimiento",
                        "cliente_postventa", "otros_contactos", "fuera_de_alcance",
                        "posible_spam",
                    ],
                    "description": "Categoria principal segun el brief (seccion 5).",
                },
                "etapa": {
                    "type": "string",
                    "enum": [
                        "pendiente_de_informacion", "datos_recibidos",
                        "pendiente_de_canalizacion", "pendiente_de_contacto_humano",
                        "cita_solicitada", "seguimiento_en_curso", "sin_respuesta",
                    ],
                    "description": "Etapa operativa. 'canalizado' y 'resuelto' no existen aqui: solo una persona los confirma.",
                },
                "queja": {
                    "type": "boolean",
                    "description": "true si hay queja o asunto importante pendiente (alerta roja, prioridad urgente).",
                },
                "prioridad": {"type": "string", "enum": ["normal", "urgente"]},
                "nombre": {"type": "string", "description": "Nombre del contacto, si lo dio."},
                "destino": {
                    "type": "string",
                    "description": "A quien corresponde segun el directorio del brief, por ejemplo: Postventa, Arquitectos y proyectos grandes (56 5703 7023), Quetzal USA, Equipo de ventas Mexico.",
                },
                "resumen": {"type": "string", "description": "Resumen fiel y breve de lo que pide la persona."},
                "datos": {
                    "type": "object",
                    "description": "Datos conocidos de la seccion 6 del brief: nombre_completo, correo, telefono_contacto, perfil, tipo_de_proyecto, ciudad, cocina_quetzal, titular_proyecto, antiguedad_cocina, ejecutivo_actual, inversion_inicial_explicada, monto_entrada_mencionado, preferencia_horario, etc. Solo lo que se sabe.",
                    "additionalProperties": True,
                },
                "datos_faltantes": {"type": "string", "description": "Que datos aun faltan."},
                "siguiente_accion": {"type": "string", "description": "Que debe pasar despues (por ejemplo: coordinar llamada, contactar al cliente)."},
            },
            "required": ["categoria", "etapa", "resumen"],
        },
    }
]


def estado_horario(ahora: datetime | None = None) -> tuple[str, bool]:
    """
    Fecha y hora de Ciudad de Mexico, y si el equipo humano esta en horario.
    Lunes a viernes 10:00 a 19:00; sabado 10:00 a 16:30; domingo cerrado.
    """
    ahora = (ahora or datetime.now(ZONA)).astimezone(ZONA)
    minutos = ahora.hour * 60 + ahora.minute
    if ahora.weekday() <= 4:
        abierto = 10 * 60 <= minutos < 19 * 60
    elif ahora.weekday() == 5:
        abierto = 10 * 60 <= minutos < 16 * 60 + 30
    else:
        abierto = False
    texto = (
        f"{DIAS[ahora.weekday()]} {ahora.day} de {MESES[ahora.month - 1]} de {ahora.year}, "
        f"{ahora.strftime('%H:%M')} (hora de Ciudad de México)"
    )
    return texto, abierto


def _contexto_dinamico(ficha: dict | None) -> str:
    """Lo que cambia en cada mensaje: hora, horario y lo que ya se sabe de esta persona."""
    texto, abierto = estado_horario()
    partes = [
        "## Contexto de este momento",
        f"Ahora es {texto}.",
        "El equipo humano de Quetzal "
        + ("está en horario de atención ahora." if abierto else "está fuera de horario ahora. No prometas cuándo contestará una persona."),
    ]
    if ficha:
        partes.append(
            "Ya hay un registro previo de esta persona. No le vuelvas a pedir lo que ya dio:\n"
            + json.dumps(ficha, ensure_ascii=False)
        )
    else:
        partes.append("Es la primera vez que esta persona escribe (no hay registro previo).")
    return "\n".join(partes)


async def _ejecutar_herramienta(nombre: str, entrada: dict, telefono: str) -> str:
    """Corre una herramienta y devuelve el resultado como texto para Claude."""
    if nombre != "registrar_conversacion":
        return f"Herramienta desconocida: {nombre}"
    if not telefono:
        return "Registro omitido: no hay identificador de conversacion."
    try:
        r = await guardar_conversacion(telefono, entrada)
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo guardar la ficha de {telefono}: {e}")
        return "No se pudo guardar el registro. Sigue con tu respuesta al cliente."
    return (
        "Registro guardado para revision del equipo. Esto NO canaliza ni envia nada: "
        "no le digas al cliente que ya se transfirio. Responde ahora al cliente."
        + (" Queja marcada como urgente." if r.get("queja") else "")
    )


async def generar_respuesta(
    mensaje: str, historial: list[dict], telefono: str = ""
) -> tuple[str, bool]:
    """
    Genera una respuesta con Claude y registra la conversacion.

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]
        telefono: identificador de la conversacion (para la ficha interna)

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial.
    """
    global _soporta_esfuerzo

    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    ficha = None
    if telefono:
        try:
            ficha = await obtener_ficha(telefono)
        except Exception as e:  # noqa: BLE001
            logger.error(f"No se pudo leer la ficha de {telefono}: {e}")

    # El bloque grande y fijo va con cache: las llamadas siguientes lo leen mucho mas
    # barato. El bloque de contexto cambia en cada mensaje, por eso va aparte y sin cache.
    system = [
        {"type": "text", "text": cargar_system_prompt(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _contexto_dinamico(ficha)},
    ]

    async def _llamar(parametros_extra: dict):
        return await client.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=HERRAMIENTAS,
            messages=mensajes,
            **parametros_extra,
        )

    texto = ""
    for ronda in range(MAX_RONDAS):
        extras = {"output_config": {"effort": ESFUERZO}} if (_soporta_esfuerzo and ESFUERZO) else {}
        try:
            respuesta = await _llamar(extras)
        except Exception as e:  # noqa: BLE001
            if extras and _es_error_de_esfuerzo(e):
                logger.warning(
                    f"El modelo {MODELO} no acepta output_config.effort; se reintenta sin ese parametro."
                )
                _soporta_esfuerzo = False
                try:
                    respuesta = await _llamar({})
                except Exception as e2:  # noqa: BLE001
                    logger.error(f"Error llamando a Claude: {e2}")
                    return obtener_mensaje_error(), False
            else:
                logger.error(f"Error llamando a Claude: {e}")
                return obtener_mensaje_error(), False

        if getattr(respuesta, "stop_reason", None) == "max_tokens":
            logger.warning(
                f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
                "Si pasa seguido, sube ANTHROPIC_MAX_TOKENS."
            )

        texto = _extraer_texto(respuesta) or texto
        logger.info(
            f"Respuesta con {MODELO} ({respuesta.usage.input_tokens} in / "
            f"{respuesta.usage.output_tokens} out)"
        )

        llamadas = [b for b in respuesta.content if b.type == "tool_use"]
        if not llamadas:
            break

        # Se ejecutan SIEMPRE las herramientas, aunque el texto ya este listo
        resultados = []
        for llamada in llamadas:
            salida = await _ejecutar_herramienta(llamada.name, llamada.input or {}, telefono)
            resultados.append({"type": "tool_result", "tool_use_id": llamada.id, "content": salida})

        # Si Claude ya escribio la respuesta junto con el registro, no hace falta otra vuelta
        if _extraer_texto(respuesta):
            break

        mensajes.append({"role": "assistant", "content": respuesta.content})
        mensajes.append({"role": "user", "content": resultados})

    if not texto:
        logger.warning("Claude devolvio una respuesta sin texto")
        return obtener_mensaje_fallback(), False

    return texto, True
