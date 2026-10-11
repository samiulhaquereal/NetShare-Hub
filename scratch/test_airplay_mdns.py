import socket
import time
from zeroconf import Zeroconf, ServiceInfo

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

try:
    local_ip = get_local_ip()
    zc = Zeroconf()
    hostname = socket.gethostname()
    service_name = f"NetShare Hub ({hostname})._airplay._tcp.local."
    info = ServiceInfo(
        "_airplay._tcp.local.",
        service_name,
        addresses=[socket.inet_aton(local_ip)],
        port=5000,
        properties={
            b'deviceid': b'A1:B2:C3:D4:E5:F6',
            b'features': b'0x5A7FFFF7,0x1E',
            b'model': b'AppleTV3,2',
            b'srcvers': b'220.68'
        },
        server=f"{hostname}.local."
    )
    zc.register_service(info)
    print(f"AirPlay mDNS service registered on {local_ip}:5000 successfully!")
    zc.close()
except Exception as e:
    print(f"AirPlay mDNS error: {e}")
