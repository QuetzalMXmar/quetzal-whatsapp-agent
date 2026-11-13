"""
Prueba la logica del agente SIN llamar a Claude ni a WhatsApp: usa un Claude simulado.
Verifica: registro de conversacion, queja que no se apaga, mensajes separados, panel,
horario de Ciudad de Mexico y que no se mande nada si falla algo.
Correr:  python3 -m pytest tests/test_logica.py -q   (o  python3 tests/test_logica.py)
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/t.db"
os.environ["ANTHROPIC_API_KEY"] = "sk-test"
os.environ["WHATSAPP_PROVIDER"] = "zernio"
os.environ["ZERNIO_API_KEY"] = "sk_test"
os.environ["ZERNIO_WEBHOOK_SECRET"] = "secreto"
os.environ["PANEL_CLAVE"] = "clave-de-prueba-larga-123"
os.environ["PAUSA_ENTRE_MENSAJES"] = "0"

from agent import brain, memory  # noqa: E402


def bloque_texto(t):
    return NS(type="text", text=t)


def bloque_tool(i, **entrada):
    return NS(type="tool_use", id=f"tu_{i}", name="registrar_conversacion", input=entrada)


def resp(bloques, stop="end_turn"):
    return NS(content=bloques, stop_reason=stop, usage=NS(input_tokens=10, output_tokens=5))


class ClaudeFalso:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []
        self.messages = NS(create=self._crear)

    async def _crear(self, **kw):
        self.llamadas.append(kw)
        return self.respuestas.pop(0)


async def correr():
    await memory.inicializar_db()

    # 1) Texto + registro en la misma respuesta: una sola llamada, el texto sale completo
    brain.client = ClaudeFalso([resp([
        bloque_texto("Hola, ¿qué tal? Claro, te platico.\n[[MENSAJE]]\n¿Me compartirías, por favor, tus datos?"),
        bloque_tool(1, categoria="cliente_potencial", etapa="pendiente_de_informacion",
                    resumen="Quiere una cocina", nombre="Sofía", datos={"ciudad": "Puebla"}),
    ], "tool_use")])
    texto, real = await brain.generar_respuesta("Hola quiero una cocina", [], "5215500000001")
    assert real and "[[MENSAJE]]" in texto, texto
    assert len(brain.client.llamadas) == 1
    f = await memory.obtener_ficha("5215500000001")
    assert f["categoria"] == "cliente_potencial" and f["datos"]["ciudad"] == "Puebla", f

    # El system prompt va con cache y el contexto dinamico aparte
    sysm = brain.client.llamadas[0]["system"]
    assert sysm[0]["cache_control"] == {"type": "ephemeral"} and "Contexto de este momento" in sysm[1]["text"]
    assert "primera vez" in sysm[1]["text"]

    # 2) Solo registro primero, texto despues: dos llamadas
    brain.client = ClaudeFalso([
        resp([bloque_tool(2, categoria="cliente_postventa", etapa="pendiente_de_canalizacion",
                          resumen="Falla el horno", destino="Postventa")], "tool_use"),
        resp([bloque_texto("Claro, te ponemos en contacto con servicio postventa.")]),
    ])
    texto, real = await brain.generar_respuesta("Mi horno no funciona", [], "5215500000002")
    assert real and "postventa" in texto and len(brain.client.llamadas) == 2
    seg = brain.client.llamadas[1]["messages"][-1]["content"][0]
    assert seg["type"] == "tool_result" and "NO canaliza" in seg["content"]

    # 3) La queja no se apaga y fuerza prioridad urgente; datos se mezclan, no se pisan
    await memory.guardar_conversacion("5215500000003", {"categoria": "cliente_en_seguimiento", "etapa": "pendiente_de_contacto_humano",
                                      "resumen": "Nadie responde", "queja": True, "datos": {"titular": "Gerardo"}})
    await memory.guardar_conversacion("5215500000003", {"categoria": "cliente_en_seguimiento", "etapa": "seguimiento_en_curso",
                                      "resumen": "Sigue sin respuesta", "queja": False, "prioridad": "normal", "datos": {"ejecutivo_actual": "X"}})
    f = await memory.obtener_ficha("5215500000003")
    assert f["queja"] and f["prioridad"] == "urgente" and f["datos"] == {"titular": "Gerardo", "ejecutivo_actual": "X"}, f

    # 4) Error de Claude: aviso tecnico, no se guarda como conversacion real
    class Roto:
        messages = NS(create=None)
    async def boom(**kw):
        raise RuntimeError("caido")
    brain.client = NS(messages=NS(create=boom))
    texto, real = await brain.generar_respuesta("Hola", [], "5215500000004")
    assert not real and "10:00" in texto

    # 5) Horario de Ciudad de Mexico
    mx = ZoneInfo("America/Mexico_City")
    assert brain.estado_horario(datetime(2026, 11, 13, 14, 30, tzinfo=mx))[1] is True      # viernes 14:30
    assert brain.estado_horario(datetime(2026, 11, 13, 19, 0, tzinfo=mx))[1] is False      # viernes 19:00
    assert brain.estado_horario(datetime(2026, 11, 14, 16, 29, tzinfo=mx))[1] is True      # sabado 16:29
    assert brain.estado_horario(datetime(2026, 11, 14, 16, 30, tzinfo=mx))[1] is False     # sabado 16:30
    assert brain.estado_horario(datetime(2026, 11, 15, 12, 0, tzinfo=mx))[1] is False      # domingo
    # 18:00 UTC del viernes = 12:00 en CDMX, abierto
    assert brain.estado_horario(datetime(2026, 11, 13, 18, 0, tzinfo=ZoneInfo("UTC")))[1] is True

    print("logica OK")


async def correr_servidor():
    from fastapi.testclient import TestClient
    from agent import main

    enviados = []

    class ProveedorFalso:
        async def verificar_conexion(self):
            return True, "ok"
        async def enviar_mensaje(self, tel, texto, ctx=None):
            enviados.append((tel, texto))
            return True

    main.proveedor = ProveedorFalso()

    # division de mensajes
    assert main.dividir_partes("a\n[[MENSAJE]]\nb\n[[MENSAJE]]\nc\n[[MENSAJE]]\nd") == ["a", "b", "c\n\nd"]
    assert main.dividir_partes("solo uno") == ["solo uno"]
    assert "[[" not in main.unir_partes("a\n[[MENSAJE]]\nb")

    msg = main.MensajeEntrante(telefono="521550000", texto="x", mensaje_id="m1", es_propio=False)
    assert await main.enviar_en_partes(msg, "uno\n[[MENSAJE]]\ndos")
    assert [t for _, t in enviados] == ["uno", "dos"], enviados

    # panel
    with TestClient(main.app) as c:
        assert c.get("/panel").status_code == 401
        assert c.get("/panel?clave=mala").status_code == 401
        r = c.get("/panel?clave=clave-de-prueba-larga-123")
        assert r.status_code == 200 and "QUEJA / URGENTE" in r.text
        assert "Sofía" in r.text
        # escapado de HTML: un resumen malicioso no se ejecuta
        await memory.guardar_conversacion("5215500000009", {"categoria": "otros_contactos", "etapa": "datos_recibidos", "resumen": "<script>alert(1)</script>"})
        r = c.get("/panel?clave=clave-de-prueba-larga-123")
        assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text
        # marcar atendida
        r = c.post("/panel/atendida", data={"clave": "clave-de-prueba-larga-123", "telefono": "5215500000009", "atendida": "1"}, follow_redirects=False)
        assert r.status_code == 303
        r = c.post("/panel/atendida", data={"clave": "mala", "telefono": "5215500000009"}, follow_redirects=False)
        assert r.status_code == 401
    print("servidor OK")


if __name__ == "__main__":
    asyncio.run(correr())
    asyncio.run(correr_servidor())
