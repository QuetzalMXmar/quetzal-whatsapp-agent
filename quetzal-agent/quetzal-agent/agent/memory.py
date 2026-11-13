# agent/memory.py — Memoria de conversaciones
# Generado por AgentKit

"""
Guarda el historial de cada conversacion por numero de telefono, y lleva registro de
que eventos de webhook ya se atendieron.

SQLite en local, PostgreSQL en produccion.
"""

import logging
import os
import json
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import Boolean, DateTime, Integer, String, Text, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Railway entrega la URL de PostgreSQL con el esquema "postgresql://" (o "postgres://").
# SQLAlchemy en modo asincrono necesita que el driver sea explicito.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# En produccion, SQLite vive dentro del contenedor y el disco del contenedor es efimero:
# cada redespliegue borra el historial de todas las conversaciones. Avisarlo fuerte, porque
# el agente arranca igual y el problema recien se nota cuando un cliente vuelve a escribir.
if DATABASE_URL.startswith("sqlite") and os.getenv("ENVIRONMENT") == "production":
    logger.warning(
        "Estas en produccion con SQLite. El historial se va a borrar en cada redespliegue. "
        "Agrega PostgreSQL y configura DATABASE_URL para que el agente recuerde a sus clientes."
    )

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ahora() -> datetime:
    """Hora actual en UTC, con zona horaria."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Un mensaje del historial de conversacion."""

    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EventoProcesado(Base):
    """
    Eventos de webhook que ya se atendieron.

    Los proveedores entregan "al menos una vez": el mismo evento puede llegar dos veces.
    Sin esta tabla, el cliente recibiria la misma respuesta repetida.
    """

    __tablename__ = "eventos_procesados"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


class Conversacion(Base):
    """
    Ficha interna de cada conversacion: la ve el equipo humano en /panel.

    Registrar una conversacion NO la canaliza: solo la deja visible. Que se haya
    transferido de verdad a alguien lo decide una persona.
    """

    __tablename__ = "conversaciones"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(200), default="")
    categoria: Mapped[str] = mapped_column(String(60), default="por_identificar", index=True)
    etapa: Mapped[str] = mapped_column(String(60), default="pendiente_de_informacion")
    prioridad: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    queja: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    destino: Mapped[str] = mapped_column(String(120), default="")
    resumen: Mapped[str] = mapped_column(Text, default="")
    datos: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    datos_faltantes: Mapped[str] = mapped_column(Text, default="")
    siguiente_accion: Mapped[str] = mapped_column(Text, default="")
    atendida: Mapped[bool] = mapped_column(Boolean, default=False)  # la marca una persona
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def marcar_evento_procesado(evento_id: str) -> bool:
    """
    Registra un evento. Retorna True si es nuevo, False si ya se habia procesado.

    La unicidad la garantiza la base de datos (clave primaria), no una consulta previa:
    asi dos webhooks que llegan al mismo tiempo no pasan los dos.
    """
    if not evento_id:
        return True  # sin id no podemos deduplicar: se procesa

    async with async_session() as session:
        session.add(EventoProcesado(evento_id=evento_id, creado_en=ahora()))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def liberar_evento(evento_id: str):
    """
    Borra la marca de un evento para que el reintento del proveedor SI se procese.

    Se usa cuando el mensaje se marco como procesado pero despues fallo el envio de la
    respuesta. Sin esto, el reintento se descartaria por duplicado y el cliente se
    quedaria sin respuesta para siempre.
    """
    if not evento_id:
        return
    async with async_session() as session:
        await session.execute(delete(EventoProcesado).where(EventoProcesado.evento_id == evento_id))
        await session.commit()


async def limpiar_eventos_viejos(dias: int = 7):
    """Borra los eventos de hace mas de N dias para que la tabla no crezca sin fin."""
    limite = ahora() - timedelta(days=dias)
    async with async_session() as session:
        resultado = await session.execute(
            delete(EventoProcesado).where(EventoProcesado.creado_en < limite)
        )
        await session.commit()
    if resultado.rowcount:
        logger.info(f"Se limpiaron {resultado.rowcount} eventos de mas de {dias} dias")


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de esa conversacion."""
    async with async_session() as session:
        session.add(Mensaje(telefono=telefono, role=role, content=content, timestamp=ahora()))
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Devuelve los ultimos N mensajes de una conversacion, en orden cronologico.

    Se ordena por id y no por timestamp: dos mensajes guardados en el mismo instante
    tienen el mismo timestamp, y el orden entre ellos quedaria librado al azar.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.id.desc())
            .limit(limite)
        )
        mensajes = list(resultado.scalars().all())

    mensajes.reverse()  # vienen del mas nuevo al mas viejo: los damos vuelta

    # La API de Claude exige que el historial empiece con un mensaje del usuario.
    # Si por un error anterior quedo un "assistant" suelto al principio, lo sacamos.
    while mensajes and mensajes[0].role != "user":
        mensajes.pop(0)

    return [{"role": m.role, "content": m.content} for m in mensajes]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversacion."""
    async with async_session() as session:
        await session.execute(delete(Mensaje).where(Mensaje.telefono == telefono))
        await session.commit()


async def guardar_conversacion(telefono: str, campos: dict) -> dict:
    """
    Crea o actualiza la ficha de una conversacion.

    Reglas: una queja, una vez marcada, no se apaga sola (la cierra una persona), y
    los datos nuevos se mezclan con los que ya habia en vez de pisarlos.
    """
    async with async_session() as session:
        c = await session.get(Conversacion, telefono)
        nueva = c is None
        if nueva:
            c = Conversacion(telefono=telefono, creado_en=ahora())
            session.add(c)

        for clave in ("nombre", "categoria", "etapa", "destino", "resumen",
                      "datos_faltantes", "siguiente_accion"):
            valor = campos.get(clave)
            if valor:
                setattr(c, clave, str(valor)[:4000])

        if campos.get("queja"):
            c.queja = True
        # Una queja siempre es urgente; el agente no la puede bajar de prioridad
        c.prioridad = "urgente" if c.queja else (campos.get("prioridad") or c.prioridad or "normal")

        nuevos = campos.get("datos") or {}
        if isinstance(nuevos, dict) and nuevos:
            try:
                actuales = json.loads(c.datos or "{}")
            except ValueError:
                actuales = {}
            actuales.update({k: v for k, v in nuevos.items() if v not in (None, "")})
            c.datos = json.dumps(actuales, ensure_ascii=False)

        # Si vuelve a escribir despues de estar atendida, vuelve a quedar pendiente
        c.atendida = False
        c.actualizado_en = ahora()
        await session.commit()
        return {"telefono": telefono, "nueva": nueva, "queja": c.queja, "prioridad": c.prioridad}


async def listar_conversaciones(limite: int = 200) -> list[Conversacion]:
    """Quejas primero, luego urgentes, luego lo mas reciente."""
    async with async_session() as session:
        resultado = await session.execute(
            select(Conversacion).order_by(
                Conversacion.atendida.asc(),
                Conversacion.queja.desc(),
                Conversacion.actualizado_en.desc(),
            ).limit(limite)
        )
        return list(resultado.scalars().all())


async def marcar_atendida(telefono: str, atendida: bool = True) -> bool:
    """La usa una persona del equipo desde el panel."""
    async with async_session() as session:
        c = await session.get(Conversacion, telefono)
        if c is None:
            return False
        c.atendida = atendida
        await session.commit()
        return True


async def obtener_ficha(telefono: str) -> dict | None:
    """La ficha actual de una conversacion, o None si es la primera vez que escribe."""
    async with async_session() as session:
        c = await session.get(Conversacion, telefono)
        if c is None:
            return None
        try:
            datos = json.loads(c.datos or "{}")
        except ValueError:
            datos = {}
        return {
            "nombre": c.nombre, "categoria": c.categoria, "etapa": c.etapa,
            "prioridad": c.prioridad, "queja": c.queja, "destino": c.destino,
            "resumen": c.resumen, "datos": datos, "datos_faltantes": c.datos_faltantes,
        }
