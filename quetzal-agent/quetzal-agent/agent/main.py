# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import hmac
import html
import json
import logging
import os
import re
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from agent.brain import generar_respuesta, obtener_mensaje_error
from agent.memory import (
    guardar_mensaje,
    inicializar_db,
    liberar_evento,
    listar_conversaciones,
    marcar_atendida,
    limpiar_eventos_viejos,
    marcar_evento_procesado,
    obtener_historial,
)
from agent.providers import obtener_proveedor
from agent.providers.base import MensajeEntrante

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# En desarrollo queremos el detalle de NUESTRO agente, no el de las librerias.
# Poner el nivel raiz en DEBUG llena la terminal de ruido de aiosqlite y httpx
# y hace imposible leer lo que hizo el agente.
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

PORT = int(os.getenv("PORT", "8000"))

# Un candado por numero de telefono. En WhatsApp es normal que alguien mande "hola" y
# medio segundo despues la pregunta de verdad: sin esto los dos mensajes se procesarian
# en paralelo, los dos leerian el mismo historial y las escrituras quedarian intercaladas.
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Si la configuracion esta mal, guardamos el error y lo mostramos en el health check,
# en vez de reventar en el import y dejar a Railway reiniciando el contenedor a ciegas.
proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001 — cualquier problema de configuracion
    error_configuracion = str(e)

# Resultado del chequeo de credenciales que se hace al arrancar. Se expone en el health
# check: que el servidor conteste no significa que el agente pueda responder por WhatsApp.
estado_proveedor: dict = {"ok": None, "detalle": "sin verificar"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos()
    logger.info("Base de datos lista")
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    yield


app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway y monitoreo."""
    if error_configuracion:
        return {"status": "error", "service": "agentkit", "detalle": error_configuracion}

    # Se responde 200 aunque las credenciales esten mal, para que Railway no marque el
    # deploy como caido y puedas leer el diagnostico. El detalle esta en el cuerpo.
    return {
        "status": "ok" if estado_proveedor["ok"] else "degradado",
        "service": "agentkit",
        "proveedor": proveedor.__class__.__name__ if proveedor else None,
        "conexion": estado_proveedor,
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificacion GET del webhook. La pide Meta; para Zernio no hace nada."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    respuesta = await proveedor.validar_webhook(request)
    if respuesta is not None:
        return PlainTextResponse(respuesta)

    # Meta pide un 403 cuando manda hub.mode=subscribe y el verify_token no coincide.
    # Devolverle 200 le hace creer que la URL quedo verificada cuando no es cierto.
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")

    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, tareas: BackgroundTasks):
    """
    Recibe los mensajes de WhatsApp.

    Contesta 200 de inmediato y procesa el mensaje en segundo plano.

    Esto NO es un detalle de estilo. Los proveedores esperan un 2xx en unos 5 segundos y,
    si no lo reciben, reintentan el mismo evento hasta 7 veces. Como llamar a Claude tarda
    mas que eso, procesar antes de contestar hace que el cliente reciba la misma respuesta
    repetida. Por eso: responder primero, trabajar despues.
    """
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    if not await proveedor.verificar_firma(request):
        raise HTTPException(status_code=401, detail="Firma del webhook invalida")

    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:  # noqa: BLE001
        # Un payload raro no debe hacer que el proveedor reintente para siempre
        logger.error(f"No se pudo leer el webhook: {e}")
        return {"status": "ignorado"}

    encolados = 0
    for msg in mensajes:
        if msg.es_propio:
            continue
        if not msg.texto.strip():
            logger.info(f"Mensaje sin texto de {msg.telefono} (archivo, imagen o reaccion): se ignora")
            continue

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
        tareas.add_task(procesar_mensaje, msg)
        encolados += 1

    return {"status": "ok", "encolados": encolados}


SEPARADOR = "[[MENSAJE]]"
PAUSA_ENTRE_MENSAJES = float(os.getenv("PAUSA_ENTRE_MENSAJES") or "1.5")


def dividir_partes(respuesta: str) -> list[str]:
    """Parte la respuesta en los mensajes que pidio el agente (maximo 3)."""
    partes = [p.strip() for p in respuesta.split(SEPARADOR)]
    partes = [p for p in partes if p]
    if len(partes) > 3:  # por si se pasa: lo que sobre se pega al ultimo
        partes = partes[:2] + ["\n\n".join(partes[2:])]
    return partes or [respuesta.replace(SEPARADOR, "").strip()]


def unir_partes(respuesta: str) -> str:
    """Para el historial: la conversacion queda como texto corrido, sin separadores."""
    return "\n\n".join(dividir_partes(respuesta))


async def enviar_en_partes(msg: MensajeEntrante, respuesta: str) -> bool:
    """Manda cada parte como un mensaje distinto, con una pausa corta entre ellos."""
    partes = dividir_partes(respuesta)
    for i, parte in enumerate(partes):
        if i:
            await asyncio.sleep(PAUSA_ENTRE_MENSAJES)
        if not await proveedor.enviar_mensaje(msg.telefono, parte, msg.contexto):
            # Si ya salio alguna parte, no se reintenta todo: se duplicaria lo ya enviado
            return i > 0
    return True


async def procesar_mensaje(msg: MensajeEntrante):
    """
    Genera la respuesta y la manda de vuelta. Corre fuera del ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id

    async with _candados[msg.telefono]:
        try:
            # El historial se lee ANTES de guardar el mensaje actual: brain.py agrega
            # el mensaje nuevo al final, y asi no queda duplicado.
            historial = await obtener_historial(msg.telefono)
            respuesta, es_respuesta_real = await generar_respuesta(msg.texto, historial, msg.telefono)

            enviado = await enviar_en_partes(msg, respuesta)

            if not enviado:
                # El evento se marco como procesado ANTES de llegar hasta aca, para que dos
                # entregas simultaneas no se dupliquen. Si el envio fallo, hay que soltarlo:
                # si no, el reintento del proveedor se descartaria por duplicado y el cliente
                # se quedaria sin respuesta para siempre.
                logger.error(f"No se pudo enviar la respuesta a {msg.telefono}; se libera el evento")
                await liberar_evento(evento_id)
                return

            # Solo se guarda en el historial lo que de verdad es conversacion. Los avisos
            # tecnicos ("estoy teniendo problemas") no son un turno del agente: guardarlos
            # los deja contaminando el contexto de todos los mensajes que vengan despues.
            if es_respuesta_real:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", unir_partes(respuesta))

            logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono}: {e}")
            await liberar_evento(evento_id)
            try:
                await proveedor.enviar_mensaje(msg.telefono, obtener_mensaje_error(), msg.contexto)
            except Exception:  # noqa: BLE001
                logger.error("Tampoco se pudo avisarle al cliente del error")


# ── Panel interno ─────────────────────────────────────────────────────────
# Lo ve el equipo de Quetzal para saber quien escribio y que sigue pendiente.
# Se protege con PANEL_CLAVE; si no esta definida, el panel queda apagado.

PANEL_CLAVE = os.getenv("PANEL_CLAVE", "")

ETIQUETAS = {
    "por_identificar": "Por identificar",
    "cliente_potencial": "Cliente potencial",
    "cliente_en_seguimiento": "Cliente en seguimiento",
    "cliente_postventa": "Cliente postventa",
    "otros_contactos": "Otros contactos",
    "fuera_de_alcance": "Fuera de alcance",
    "posible_spam": "Posible spam (revisar)",
}


def _revisar_clave(clave: str):
    if not PANEL_CLAVE:
        raise HTTPException(status_code=503, detail="Panel apagado: falta PANEL_CLAVE")
    if not hmac.compare_digest(clave.encode(), PANEL_CLAVE.encode()):
        raise HTTPException(status_code=401, detail="Clave incorrecta")


def _e(valor) -> str:
    return html.escape(str(valor or ""))


@app.get("/panel", response_class=HTMLResponse)
async def panel(clave: str = ""):
    _revisar_clave(clave)
    convs = await listar_conversaciones()
    pendientes = [c for c in convs if not c.atendida]
    atendidas = [c for c in convs if c.atendida]

    def tarjeta(c) -> str:
        try:
            datos = json.loads(c.datos or "{}")
        except ValueError:
            datos = {}
        lineas = "".join(f"<div><span class='k'>{_e(k)}</span> {_e(v)}</div>" for k, v in datos.items())
        tel = re.sub(r"\D", "", c.telefono)
        contacto = f"<a href='https://wa.me/{tel}'>Abrir chat</a>" if tel else ""
        alerta = "<span class='alerta'>QUEJA / URGENTE</span>" if c.queja else (
            "<span class='urg'>URGENTE</span>" if c.prioridad == "urgente" else "")
        boton = (
            f"<form method='post' action='/panel/atendida'>"
            f"<input type='hidden' name='clave' value='{_e(clave)}'>"
            f"<input type='hidden' name='telefono' value='{_e(c.telefono)}'>"
            f"<input type='hidden' name='atendida' value='{'0' if c.atendida else '1'}'>"
            f"<button>{'Reabrir' if c.atendida else 'Marcar atendida'}</button></form>"
        )
        return (
            f"<article class='{'queja' if c.queja else ''}'>"
            f"<header><b>{_e(c.nombre) or 'Sin nombre'}</b> <span class='tel'>{_e(c.telefono)}</span> {alerta}</header>"
            f"<div class='meta'>{_e(ETIQUETAS.get(c.categoria, c.categoria))} · {_e(c.etapa.replace('_', ' '))}"
            f"{' · ' + _e(c.destino) if c.destino else ''}</div>"
            f"<p>{_e(c.resumen)}</p>"
            f"{lineas}"
            f"{'<div><span class=k>falta</span> ' + _e(c.datos_faltantes) + '</div>' if c.datos_faltantes else ''}"
            f"{'<div><span class=k>sigue</span> ' + _e(c.siguiente_accion) + '</div>' if c.siguiente_accion else ''}"
            f"<footer>{contacto}{boton}<span class='fecha'>{c.actualizado_en:%d/%m %H:%M} UTC</span></footer>"
            f"</article>"
        )

    cuerpo = "".join(tarjeta(c) for c in pendientes) or "<p class='vacio'>Nada pendiente.</p>"
    cerradas = "".join(tarjeta(c) for c in atendidas[:30])
    pagina = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Quetzal · Conversaciones</title>
<style>
body{{margin:0;background:#E6E5E2;color:#0D1623;font:16px/1.5 Avantt,Inter,'Helvetica Neue',Arial,sans-serif;padding:48px 6vw}}
h1{{font-weight:400;font-size:32px;margin:0 0 4px}} h2{{font:12px Nord,'Space Grotesk',Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase;margin:48px 0 16px;color:#212E40}}
.sub{{color:#212E40;margin-bottom:32px}}
article{{padding:20px 0 28px;margin-bottom:12px}} article.queja{{border-left:4px solid #B3261E;padding-left:16px}}
header{{font-size:18px}} .tel,.fecha,.k{{color:#695F53;font-size:13px}} .k{{display:inline-block;min-width:64px}}
.meta{{color:#212E40;font-size:14px;margin:2px 0 8px}} p{{margin:8px 0}}
.alerta{{color:#B3261E;font:12px Nord,Arial,sans-serif;letter-spacing:.12em;margin-left:8px}} .urg{{color:#695F53;font-size:12px;letter-spacing:.12em;margin-left:8px}}
footer{{display:flex;gap:20px;align-items:center;margin-top:12px}} footer a{{color:#0D1623}} form{{margin:0}}
button{{background:none;border:0;color:#0D1623;font:inherit;text-decoration:underline;cursor:pointer;padding:0}}
.vacio{{color:#212E40}}
</style></head><body>
<h1>Conversaciones</h1><div class="sub">{len(pendientes)} pendientes. Las quejas van primero.</div>
{cuerpo}
{'<h2>Atendidas</h2>' + cerradas if cerradas else ''}
</body></html>"""
    return HTMLResponse(pagina)


@app.post("/panel/atendida")
async def panel_atendida(clave: str = Form(""), telefono: str = Form(""), atendida: str = Form("1")):
    _revisar_clave(clave)
    await marcar_atendida(telefono, atendida == "1")
    return RedirectResponse(url=f"/panel?clave={clave}", status_code=303)
