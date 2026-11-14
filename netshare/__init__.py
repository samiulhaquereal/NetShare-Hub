"""RetCore OnLAN — screen streaming, file sharing, clipboard sync and chat over LAN.

Package layout (dependencies only point downwards):

    ui/              Tkinter host control panel        ─┐
    web/             Flask routes, WebSocket handlers   ├─ presentation
    services/        Application use-cases             ─── application
    infrastructure/  OS, DB, capture, ADB, ngrok …     ─── adapters
    domain/          Pure types and protocol constants ─── domain
    container.py     Composition root wiring it all together
"""

__version__ = "2.1.0"
