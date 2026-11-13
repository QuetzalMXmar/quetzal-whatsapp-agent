# Agente de WhatsApp de Quetzal

Contesta los mensajes nuevos con el brief de Quetzal, clasifica cada chat y deja todo visible en un panel para el equipo.

## Qué hace y qué no

Hace:
- Responde con el tono y la información del brief (knowledge/Quetzal_Brief_Agente_WhatsApp.md)
- Separa las respuestas largas en 2 o 3 mensajes
- Clasifica cada conversación: categoría, etapa, prioridad y quejas en alerta roja
- Sabe la hora de Ciudad de México y si el equipo está en horario
- No pide otra vez los datos que el cliente ya dio

No hace todavía:
- No avisa a nadie por su cuenta. Todo queda en el panel (/panel) y una persona lo revisa. Por eso el agente nunca le dice al cliente que ya se transfirió su caso.
- No lee PDFs ni imágenes. Si llega solo un archivo sin texto, no contesta.
- No manda seguimientos automáticos.

## Para cambiar lo que dice el agente

1. Edita knowledge/Quetzal_Brief_Agente_WhatsApp.md
2. Regenera el prompt (el script está en scripts/build_prompt.py)
3. Sube el cambio a GitHub y Railway lo despliega solo

## Variables que va a pedir Railway

ANTHROPIC_API_KEY, WHATSAPP_PROVIDER=zernio, ZERNIO_API_KEY, ZERNIO_WEBHOOK_SECRET, PANEL_CLAVE, ENVIRONMENT=production, DATABASE_URL (la da el PostgreSQL de Railway).

Nunca subas el archivo .env a GitHub.

## Pruebas

- python3 tests/test_logica.py : revisa la lógica sin gastar nada
- python3 tests/test_casos.py : corre los casos del brief contra Claude de verdad (usa tu clave, cuesta centavos)
