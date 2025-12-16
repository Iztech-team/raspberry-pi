import os
import uuid
import base64
import binascii
import tempfile
import socket
import time
from typing import Tuple, Optional

import cups
from fastapi import FastAPI, UploadFile, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from PIL import Image

# ESC/POS library - generate commands, CUPS handles spooling
from escpos.printer import Dummy
from escpos.exceptions import Error as EscposError

# Load environment variables
load_dotenv()

# Configuration
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "bmp", "gif"}
MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_SIZE_MB", "20")) * 1024 * 1024
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "3006"))

# Retry configuration for WiFi printers
PRINT_MAX_RETRIES = int(os.getenv("PRINT_MAX_RETRIES", "3"))
PRINT_RETRY_DELAY = float(os.getenv("PRINT_RETRY_DELAY", "2.0"))  # seconds
PRINTER_TIMEOUT = float(os.getenv("PRINTER_TIMEOUT", "5.0"))  # connection check timeout

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ============== STARTUP EVENT ==============
@app.on_event("startup")
async def startup_event():
    """
    On server startup, try to enable all printers.
    This helps recover from power outages where CUPS may have paused queues.
    """
    print("[STARTUP] Checking and enabling printer queues...")
    try:
        printers = list(cups.Connection().getPrinters().keys())
        for printer_name in printers:
            try:
                conn = cups.Connection()
                conn.enablePrinter(printer_name)
                conn.acceptJobs(printer_name)
                print(f"[STARTUP] Enabled printer: {printer_name}")
            except Exception as e:
                print(f"[STARTUP] Could not enable {printer_name}: {e}")
    except Exception as e:
        print(f"[STARTUP] CUPS not available: {e}")
    print("[STARTUP] Printer check complete")


# ============== GLOBAL ERROR HANDLERS ==============
# These catch ALL errors so the server never crashes!

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all handler for any unhandled exception - server stays alive!"""
    print(f"[ERROR] Unhandled exception: {type(exc).__name__}: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "detail": str(exc),
            "type": type(exc).__name__
        }
    )


@app.exception_handler(EscposError)
async def escpos_exception_handler(request: Request, exc: EscposError):
    """Handle printer-specific errors"""
    print(f"[PRINTER ERROR] {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Printer error",
            "detail": str(exc),
            "type": "PrinterError"
        }
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle invalid request parameters"""
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": "Validation error",
            "detail": exc.errors(),
            "type": "ValidationError"
        }
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions consistently"""
    print(f"[HTTP ERROR] {exc.status_code}: {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
            "status_code": exc.status_code,
            "type": "HTTPException"
        }
    )

# ============== END ERROR HANDLERS ==============


def allowed_file(filename: str) -> bool:
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def list_cups_printers() -> list:
    """Return available CUPS printers (queues) dynamically."""
    try:
        return list(cups.Connection().getPrinters().keys())
    except cups.IPPError as e:
        print(f"[CUPS ERROR] IPP error listing printers: {e}")
        raise HTTPException(status_code=500, detail=f"CUPS IPP error: {e}")
    except Exception as e:
        print(f"[CUPS ERROR] Error listing printers: {e}")
        raise HTTPException(status_code=500, detail=f"CUPS error: {e}")


def get_printer_info(printer_name: str) -> dict:
    """
    Get detailed printer information including URI and status.
    Returns dict with 'uri', 'ip', 'port', 'state', 'state_message', 'is_accepting'.
    Handles various URI types: socket://, ipp://, ipps://, lpd://, http://
    """
    try:
        conn = cups.Connection()
        printers = conn.getPrinters()
        if printer_name not in printers:
            return None
        
        info = printers[printer_name]
        uri = info.get('device-uri', '')
        
        # Extract IP and port from various URI formats
        # socket://192.168.1.100:9100 (direct network - LAN/WiFi)
        # ipp://192.168.1.100:631/ipp/print (IPP protocol)
        # ipps://192.168.1.100:631/ipp/print (IPP over SSL)
        # lpd://192.168.1.100/queue (LPD protocol)
        # http://192.168.1.100:80/print (HTTP-based printers)
        ip = None
        port = 9100  # Default for socket://
        printer_type = 'unknown'
        
        if '://' in uri:
            scheme = uri.split('://')[0].lower()
            addr_part = uri.split('://')[1]
            
            # Remove path component (everything after first /)
            if '/' in addr_part:
                addr_part = addr_part.split('/')[0]
            
            # Handle different schemes
            if scheme == 'socket':
                printer_type = 'network_raw'
                port = 9100
            elif scheme in ('ipp', 'ipps'):
                printer_type = 'ipp'
                port = 631
            elif scheme == 'lpd':
                printer_type = 'lpd'
                port = 515
            elif scheme in ('http', 'https'):
                printer_type = 'http'
                port = 80 if scheme == 'http' else 443
            elif scheme == 'usb':
                printer_type = 'usb'
                # USB printers don't have IP
                addr_part = None
            elif scheme == 'serial':
                printer_type = 'serial'
                addr_part = None
            
            # Extract IP and custom port if present
            if addr_part:
                if ':' in addr_part:
                    ip = addr_part.split(':')[0]
                    try:
                        port = int(addr_part.split(':')[1])
                    except ValueError:
                        pass
                else:
                    ip = addr_part
                
                # Validate IP looks like an IP address (not a hostname like 'localhost')
                import re
                if ip and not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                    # It's a hostname, try to resolve it
                    try:
                        resolved_ip = socket.gethostbyname(ip)
                        ip = resolved_ip
                    except socket.gaierror:
                        # Could not resolve, keep original for logging
                        pass
        
        return {
            'name': printer_name,
            'uri': uri,
            'ip': ip,
            'port': port,
            'type': printer_type,
            'state': info.get('printer-state', 0),
            'state_message': info.get('printer-state-message', ''),
            'state_reasons': info.get('printer-state-reasons', []),
            'is_accepting': info.get('printer-is-accepting-jobs', True),
        }
    except Exception as e:
        print(f"[ERROR] Could not get printer info for {printer_name}: {e}")
        return None


def check_printer_reachable(ip: str, port: int = 9100, timeout: float = None) -> bool:
    """
    Check if a network printer is reachable by testing TCP connection.
    This is crucial for WiFi printers that may not be ready after power outage.
    """
    if not ip:
        return True  # Can't check non-network printers, assume OK
    
    if timeout is None:
        timeout = PRINTER_TIMEOUT
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception as e:
        print(f"[PRINTER CHECK] Error checking {ip}:{port} - {e}")
        return False


def ensure_printer_ready(printer_name: str, auto_enable: bool = True) -> Tuple[bool, str]:
    """
    Ensure printer is ready to receive jobs:
    1. Check if printer exists in CUPS
    2. Check if network printer is reachable (WiFi printers!)
    3. Check if CUPS queue is accepting jobs
    4. Auto-enable paused queues if requested
    
    Returns (is_ready, message)
    """
    info = get_printer_info(printer_name)
    if not info:
        return False, f"Printer '{printer_name}' not found in CUPS"
    
    # Check network reachability for socket:// printers (WiFi printers)
    if info['ip']:
        if not check_printer_reachable(info['ip'], info['port']):
            return False, f"Printer '{printer_name}' at {info['ip']}:{info['port']} is not reachable (WiFi not connected?)"
    
    # Check CUPS queue status
    # State: 3=idle, 4=processing, 5=stopped
    if info['state'] == 5:  # Stopped
        if auto_enable:
            print(f"[PRINTER] Queue {printer_name} is stopped, attempting to enable...")
            try:
                conn = cups.Connection()
                conn.enablePrinter(printer_name)
                conn.acceptJobs(printer_name)
                print(f"[PRINTER] Successfully enabled {printer_name}")
            except Exception as e:
                return False, f"Printer queue '{printer_name}' is stopped and could not be enabled: {e}"
        else:
            return False, f"Printer queue '{printer_name}' is stopped"
    
    # Check if accepting jobs
    if not info['is_accepting']:
        if auto_enable:
            print(f"[PRINTER] Queue {printer_name} not accepting jobs, attempting to enable...")
            try:
                conn = cups.Connection()
                conn.acceptJobs(printer_name)
                print(f"[PRINTER] {printer_name} now accepting jobs")
            except Exception as e:
                return False, f"Printer '{printer_name}' not accepting jobs and could not be enabled: {e}"
        else:
            return False, f"Printer '{printer_name}' is not accepting jobs"
    
    return True, "Printer ready"


def get_printer_queue(printer_name: str) -> str:
    """Return the CUPS queue name after validating it exists."""
    printers = list_cups_printers()
    if printer_name not in printers:
        available = ", ".join(printers) if printers else "none configured"
        error_msg = f"Printer '{printer_name}' not found. Available printers: {available}"
        print(f"[PRINTER ERROR] {error_msg}")
        raise HTTPException(status_code=404, detail=error_msg)
    return printer_name


def create_printer(printer_name: str) -> Tuple[Dummy, str]:
    """Create a Dummy ESC/POS printer and return it with the target CUPS queue."""
    queue = get_printer_queue(printer_name)
    return Dummy(), queue


def collect_output_bytes(printer_obj: Dummy) -> bytes:
    """
    Extract the generated ESC/POS bytes from a Dummy printer.
    The Dummy backend buffers commands instead of sending them directly.
    """
    output = getattr(printer_obj, "output", None)
    if output is None:
        raise HTTPException(status_code=500, detail="No ESC/POS data generated")
    if isinstance(output, (bytes, bytearray)):
        return bytes(output)
    if hasattr(output, "getvalue"):
        return output.getvalue()
    raise HTTPException(status_code=500, detail="Unable to read ESC/POS buffer")


def send_to_cups(queue_name: str, data: bytes, title: str, retry: bool = True) -> int:
    """
    Send raw ESC/POS bytes to a CUPS queue as a raw job.
    
    With retry=True (default), will:
    1. Check printer is reachable first (important for WiFi printers!)
    2. Auto-enable paused CUPS queues
    3. Retry on failure with configurable delay
    """
    last_error = None
    max_attempts = PRINT_MAX_RETRIES if retry else 1
    
    for attempt in range(1, max_attempts + 1):
        try:
            # Check printer is ready (reachable + queue accepting)
            is_ready, message = ensure_printer_ready(queue_name, auto_enable=True)
            if not is_ready:
                raise HTTPException(status_code=503, detail=message)
            
            conn = cups.Connection()
            printers = conn.getPrinters()
            if queue_name not in printers:
                raise HTTPException(status_code=400, detail=f"CUPS queue '{queue_name}' not found")
            
            options = {
                "raw": "true",
                "document-format": "application/vnd.cups-raw",
            }
            
            if hasattr(conn, "printData"):
                job_id = conn.printData(queue_name, title, data, options)
            else:
                # Fallback for older pycups without printData
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp.write(data)
                    tmp.flush()
                    tmp_path = tmp.name
                try:
                    job_id = conn.printFile(queue_name, tmp_path, title, options)
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            
            if attempt > 1:
                print(f"[PRINT] Success on attempt {attempt} for {queue_name}")
            return job_id
            
        except HTTPException as e:
            last_error = e
            if attempt < max_attempts:
                print(f"[PRINT] Attempt {attempt}/{max_attempts} failed for {queue_name}: {e.detail}")
                print(f"[PRINT] Retrying in {PRINT_RETRY_DELAY}s...")
                time.sleep(PRINT_RETRY_DELAY)
            else:
                raise
        except cups.IPPError as e:
            last_error = HTTPException(status_code=500, detail=f"CUPS IPP error: {e}")
            if attempt < max_attempts:
                print(f"[PRINT] CUPS error on attempt {attempt}/{max_attempts}: {e}")
                time.sleep(PRINT_RETRY_DELAY)
            else:
                raise last_error
        except Exception as e:
            last_error = HTTPException(status_code=500, detail=f"CUPS error: {e}")
            if attempt < max_attempts:
                print(f"[PRINT] Error on attempt {attempt}/{max_attempts}: {e}")
                time.sleep(PRINT_RETRY_DELAY)
            else:
                raise last_error
    
    raise last_error


@app.get("/")
@app.get("/health")
def health():
    """Health check endpoint with printer status"""
    try:
        printers = list_cups_printers()
    except Exception as e:
        # CUPS not available - return degraded status
        return {
            "ok": False,
            "status": "degraded",
            "message": "CUPS service not available",
            "error": str(e),
            "all_printers_ready": False,
            "printers": [],
            "config": {
                "max_retries": PRINT_MAX_RETRIES,
                "retry_delay": PRINT_RETRY_DELAY,
                "timeout": PRINTER_TIMEOUT
            }
        }
    
    printer_status = []
    all_ready = True
    
    for printer_name in printers:
        try:
            info = get_printer_info(printer_name)
            if info:
                reachable = check_printer_reachable(info['ip'], info['port']) if info['ip'] else True
                ready = reachable and info['is_accepting'] and info['state'] != 5
                if not ready:
                    all_ready = False
                printer_status.append({
                    'name': printer_name,
                    'ready': ready,
                    'reachable': reachable,
                    'ip': info['ip'],
                    'type': info.get('type', 'unknown')
                })
        except Exception as e:
            print(f"[HEALTH] Error checking printer {printer_name}: {e}")
            printer_status.append({
                'name': printer_name,
                'ready': False,
                'reachable': False,
                'error': str(e)
            })
            all_ready = False
    
    return {
        "ok": True,
        "status": "running",
        "message": "Thermal Printer API with python-escpos + CUPS",
        "all_printers_ready": all_ready,
        "printers": printer_status,
        "config": {
            "max_retries": PRINT_MAX_RETRIES,
            "retry_delay": PRINT_RETRY_DELAY,
            "timeout": PRINTER_TIMEOUT
        }
    }


@app.get("/printers")
def get_printers():
    """List available printers with their status"""
    printers = list_cups_printers()
    return {"printers": printers}


@app.get("/printers/status")
def get_printers_status():
    """
    Get detailed status of all printers including reachability.
    Useful to check if WiFi printers are ready after power outage.
    """
    printers = list_cups_printers()
    status = []
    
    for printer_name in printers:
        info = get_printer_info(printer_name)
        if info:
            # Check if reachable
            reachable = True
            if info['ip']:
                reachable = check_printer_reachable(info['ip'], info['port'])
            
            status.append({
                'name': printer_name,
                'uri': info['uri'],
                'ip': info['ip'],
                'port': info['port'],
                'type': info.get('type', 'unknown'),
                'reachable': reachable,
                'state': info['state'],
                'state_message': info['state_message'],
                'is_accepting': info['is_accepting'],
                'ready': reachable and info['is_accepting'] and info['state'] != 5
            })
    
    return {'printers': status}


@app.post("/printers/{printer_name}/check")
@app.get("/printers/{printer_name}/check")
async def check_printer(printer_name: str):
    """
    Check if a specific printer is ready (reachable and queue accepting).
    Will auto-enable paused queues.
    """
    is_ready, message = ensure_printer_ready(printer_name, auto_enable=True)
    info = get_printer_info(printer_name)
    
    return {
        'printer': printer_name,
        'ready': is_ready,
        'message': message,
        'info': info
    }


@app.post("/printers/{printer_name}/test")
async def test_printer(printer_name: str):
    """
    Send a test print to verify printer is working.
    Prints a short message with timestamp.
    """
    from datetime import datetime
    
    try:
        # First check if printer is ready
        is_ready, message = ensure_printer_ready(printer_name, auto_enable=True)
        if not is_ready:
            return {
                'success': False,
                'message': message,
                'printer': printer_name
            }
        
        # Get printer info
        info = get_printer_info(printer_name)
        
        # Create test print
        p, queue = create_printer(printer_name)
        
        # Print test message
        p.set(align='center', bold=True, width=2, height=2)
        p.text("TEST PRINT\n")
        p.set()
        
        p.text("\n")
        p.set(align='center')
        p.text(f"Printer: {printer_name}\n")
        if info and info.get('ip'):
            p.text(f"IP: {info['ip']}:{info['port']}\n")
            p.text(f"Type: {info.get('type', 'unknown')}\n")
        p.text(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        p.text("\n")
        p.text("If you see this, printer is OK!\n")
        p.set()
        
        p.text('\n\n')
        p.cut()
        
        # Beep to indicate success
        p._raw(b'\x1b\x42\x02\x02')
        
        data = collect_output_bytes(p)
        job_id = send_to_cups(queue, data, title="test-print")
        
        return {
            'success': True,
            'message': f"Test print sent to {printer_name}",
            'printer': printer_name,
            'job_id': job_id,
            'printer_info': info
        }
        
    except Exception as e:
        return {
            'success': False,
            'message': str(e),
            'printer': printer_name
        }


@app.post("/printers/test-all")
async def test_all_printers():
    """
    Send a test print to ALL configured printers.
    Useful after power outage to verify all printers are working.
    """
    from datetime import datetime
    
    results = []
    printers = list_cups_printers()
    
    if not printers:
        return {
            'success': False,
            'message': 'No printers configured',
            'results': []
        }
    
    for printer_name in printers:
        try:
            # First check if printer is ready
            is_ready, message = ensure_printer_ready(printer_name, auto_enable=True)
            if not is_ready:
                results.append({
                    'printer': printer_name,
                    'success': False,
                    'message': message
                })
                continue
            
            # Get printer info
            info = get_printer_info(printer_name)
            
            # Create test print
            p, queue = create_printer(printer_name)
            
            # Print test message
            p.set(align='center', bold=True, width=2, height=2)
            p.text("TEST PRINT\n")
            p.set()
            
            p.text("\n")
            p.set(align='center')
            p.text(f"Printer: {printer_name}\n")
            if info and info.get('ip'):
                p.text(f"IP: {info['ip']}:{info['port']}\n")
                p.text(f"Type: {info.get('type', 'unknown')}\n")
            p.text(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            p.text("\n")
            p.text("If you see this, printer is OK!\n")
            p.set()
            
            p.text('\n\n')
            p.cut()
            
            # Beep to indicate success
            p._raw(b'\x1b\x42\x02\x02')
            
            data = collect_output_bytes(p)
            job_id = send_to_cups(queue, data, title="test-print")
            
            results.append({
                'printer': printer_name,
                'success': True,
                'job_id': job_id,
                'type': info.get('type', 'unknown') if info else 'unknown'
            })
            
        except Exception as e:
            results.append({
                'printer': printer_name,
                'success': False,
                'message': str(e)
            })
    
    all_success = all(r['success'] for r in results)
    
    return {
        'success': all_success,
        'message': f"Tested {len(results)} printers, {sum(1 for r in results if r['success'])} successful",
        'results': results
    }


@app.post("/printers/{printer_name}/enable")
async def enable_printer(printer_name: str):
    """
    Enable a printer queue that was paused/stopped.
    Useful after power outage when CUPS may have paused the queue.
    """
    try:
        conn = cups.Connection()
        printers = conn.getPrinters()
        
        if printer_name not in printers:
            raise HTTPException(status_code=404, detail=f"Printer '{printer_name}' not found")
        
        conn.enablePrinter(printer_name)
        conn.acceptJobs(printer_name)
        
        return {
            'success': True,
            'message': f"Printer '{printer_name}' enabled and accepting jobs",
            'printer': printer_name
        }
    except cups.IPPError as e:
        raise HTTPException(status_code=500, detail=f"CUPS error: {e}")


@app.post("/printers/enable-all")
async def enable_all_printers():
    """
    Enable all printer queues. Useful after power outage.
    """
    printers = list_cups_printers()
    results = []
    
    for printer_name in printers:
        try:
            conn = cups.Connection()
            conn.enablePrinter(printer_name)
            conn.acceptJobs(printer_name)
            results.append({'printer': printer_name, 'success': True})
        except Exception as e:
            results.append({'printer': printer_name, 'success': False, 'error': str(e)})
    
    return {'results': results}


@app.get("/jobs")
@app.get("/printers/jobs")
async def get_all_jobs():
    """
    Get all print jobs from CUPS queue.
    Useful to check if there are stuck jobs.
    """
    try:
        conn = cups.Connection()
        jobs = conn.getJobs(which_jobs='all')
        
        job_list = []
        for job_id, job_info in jobs.items():
            job_list.append({
                'job_id': job_id,
                'printer': job_info.get('job-printer-uri', '').split('/')[-1],
                'title': job_info.get('job-name', ''),
                'state': job_info.get('job-state', 0),
                'state_reasons': job_info.get('job-state-reasons', ''),
                'owner': job_info.get('job-originating-user-name', ''),
                'size': job_info.get('job-k-octets', 0),
                'time_created': job_info.get('time-at-creation', 0),
            })
        
        return {
            'success': True,
            'total_jobs': len(job_list),
            'jobs': job_list
        }
    except cups.IPPError as e:
        raise HTTPException(status_code=500, detail=f"CUPS error: {e}")


@app.get("/printers/{printer_name}/jobs")
async def get_printer_jobs(printer_name: str):
    """
    Get print jobs for a specific printer.
    """
    try:
        conn = cups.Connection()
        printers = conn.getPrinters()
        
        if printer_name not in printers:
            raise HTTPException(status_code=404, detail=f"Printer '{printer_name}' not found")
        
        jobs = conn.getJobs(which_jobs='all')
        
        job_list = []
        for job_id, job_info in jobs.items():
            # Filter by printer
            job_printer = job_info.get('job-printer-uri', '').split('/')[-1]
            if job_printer == printer_name:
                job_list.append({
                    'job_id': job_id,
                    'title': job_info.get('job-name', ''),
                    'state': job_info.get('job-state', 0),
                    'state_reasons': job_info.get('job-state-reasons', ''),
                    'owner': job_info.get('job-originating-user-name', ''),
                    'size': job_info.get('job-k-octets', 0),
                })
        
        return {
            'success': True,
            'printer': printer_name,
            'total_jobs': len(job_list),
            'jobs': job_list
        }
    except cups.IPPError as e:
        raise HTTPException(status_code=500, detail=f"CUPS error: {e}")


@app.delete("/jobs/{job_id}")
@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    """
    Cancel a specific print job.
    """
    try:
        conn = cups.Connection()
        conn.cancelJob(job_id)
        return {
            'success': True,
            'message': f"Job {job_id} cancelled",
            'job_id': job_id
        }
    except cups.IPPError as e:
        raise HTTPException(status_code=500, detail=f"CUPS error: {e}")


@app.post("/jobs/cancel-all")
@app.delete("/jobs")
async def cancel_all_jobs(printer: str = Query(None, description="Cancel jobs for specific printer only")):
    """
    Cancel all print jobs, optionally for a specific printer.
    Useful to clear stuck jobs after power outage.
    """
    try:
        conn = cups.Connection()
        jobs = conn.getJobs(which_jobs='not-completed')
        
        cancelled = []
        failed = []
        
        for job_id, job_info in jobs.items():
            # Filter by printer if specified
            if printer:
                job_printer = job_info.get('job-printer-uri', '').split('/')[-1]
                if job_printer != printer:
                    continue
            
            try:
                conn.cancelJob(job_id)
                cancelled.append(job_id)
            except Exception as e:
                failed.append({'job_id': job_id, 'error': str(e)})
        
        return {
            'success': len(failed) == 0,
            'cancelled': cancelled,
            'failed': failed,
            'total_cancelled': len(cancelled)
        }
    except cups.IPPError as e:
        raise HTTPException(status_code=500, detail=f"CUPS error: {e}")


@app.get("/printers/wait")
@app.post("/printers/wait")
async def wait_for_printers(
    printer: str = Query(None, description="Specific printer to wait for (or all if not specified)"),
    timeout: int = Query(30, description="Maximum seconds to wait"),
    interval: float = Query(2.0, description="Check interval in seconds")
):
    """
    Wait for printer(s) to become ready. Useful for iOS app to call after power outage.
    Will keep checking until printer is reachable or timeout.
    
    Example: /printers/wait?printer=printer_1&timeout=60
    """
    start_time = time.time()
    timeout = max(5, min(120, timeout))  # Clamp between 5-120 seconds
    interval = max(0.5, min(10, interval))
    
    if printer:
        # Wait for specific printer
        printers_to_check = [printer]
    else:
        # Wait for all printers
        try:
            printers_to_check = list_cups_printers()
        except:
            return {
                'success': False,
                'message': 'CUPS not available',
                'ready': [],
                'not_ready': []
            }
    
    if not printers_to_check:
        return {
            'success': True,
            'message': 'No printers configured',
            'ready': [],
            'not_ready': []
        }
    
    ready_printers = []
    not_ready_printers = list(printers_to_check)
    attempts = 0
    
    while time.time() - start_time < timeout and not_ready_printers:
        attempts += 1
        still_not_ready = []
        
        for pname in not_ready_printers:
            is_ready, _ = ensure_printer_ready(pname, auto_enable=True)
            if is_ready:
                ready_printers.append(pname)
            else:
                still_not_ready.append(pname)
        
        not_ready_printers = still_not_ready
        
        if not_ready_printers and time.time() - start_time < timeout:
            time.sleep(interval)
    
    elapsed = round(time.time() - start_time, 1)
    all_ready = len(not_ready_printers) == 0
    
    return {
        'success': all_ready,
        'message': 'All printers ready' if all_ready else f'{len(not_ready_printers)} printer(s) not ready',
        'ready': ready_printers,
        'not_ready': not_ready_printers,
        'elapsed_seconds': elapsed,
        'attempts': attempts
    }


@app.post("/print-text")
@app.post("/print/text")
async def print_text(
    text: str = Query(..., description="Text to print"),
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    lines_after: int = Query(0, description="Feed lines before cut"),
    cut: bool = Query(True, description="Auto cut after printing"),
    bold: bool = Query(False, description="Bold text"),
    underline: int = Query(0, description="Underline mode (0=none, 1=single, 2=double)"),
    width: int = Query(1, description="Width multiplier (1-8)"),
    height: int = Query(1, description="Height multiplier (1-8)"),
    align: str = Query("left", description="Alignment (left, center, right)"),
    invert: bool = Query(False, description="Invert colors")
):
    """
    Print text to thermal printer with formatting
    
    Supports both /print-text and /print/text endpoints
    Example: /print/text?text=Hello&printer=printer_1&bold=true&width=2
    """
    # Support both 'printer' and 'printer_name' for backward compatibility
    if printer_name:
        printer = printer_name
    
    # Validate text
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    
    # Clamp values to valid ranges
    width = max(1, min(8, width))
    height = max(1, min(8, height))
    underline = max(0, min(2, underline))
    lines_after = max(0, min(255, lines_after))
    
    # Validate alignment
    if align not in ('left', 'center', 'right'):
        align = 'left'
    
    try:
        p, queue = create_printer(printer)
        
        # Set text formatting
        p.set(
            align=align,
            bold=bold,
            underline=underline,
            invert=invert,
            width=width,
            height=height
        )
        
        # Print text
        p.text(text)
        if not text.endswith('\n'):
            p.text('\n')
        
        # Reset formatting
        p.set()
        
        # Feed lines before cutting
        if lines_after > 0:
            p.text('\n' * lines_after)
        
        # Cut paper
        if cut:
            p.cut()

        # Send buffered ESC/POS to CUPS as raw
        data = collect_output_bytes(p)
        job_id = send_to_cups(queue, data, title="print-text")
        
        return {
            "success": True,
            "message": f"Text printed to {printer}",
            "printer": printer,
            "queue": queue,
            "job_id": job_id,
            "bytes": len(data),
            "lines_after": lines_after,
            "formatting": {
                "bold": bold,
                "underline": underline,
                "width": width,
                "height": height,
                "align": align
            }
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")


@app.post("/print-image")
@app.post("/print/image")
async def print_image(
    image: UploadFile,
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    lines_after: int = Query(0, description="Feed lines before cut"),
    cut: bool = Query(True, description="Auto cut after printing"),
    center: bool = Query(True, description="Center image"),
    paper_width: int = Query(510, description="Paper width in pixels (510 for 80mm, 360 for 58mm)")
):
    """
    Print image to thermal printer using python-escpos
    
    Supports both /print-image and /print/image endpoints
    Images are automatically resized to fit the paper width!
    """
    # Support both 'printer' and 'printer_name' for backward compatibility
    if printer_name:
        printer = printer_name
    
    if not image.filename:
        raise HTTPException(status_code=400, detail="No image provided")
    
    if not allowed_file(image.filename):
        raise HTTPException(status_code=400, detail=f"Invalid image type. Allowed: {ALLOWED_EXTENSIONS}")
    
    # Clamp values
    lines_after = max(0, min(255, lines_after))
    paper_width = max(200, min(600, paper_width))
    
    # Save uploaded image
    filename = secure_filename(image.filename)
    unique_filename = f"{uuid.uuid4()}_{filename}"
    filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
    
    try:
        content = await image.read()
        if len(content) == 0:
            raise HTTPException(status_code=400, detail="Empty image file")
        if len(content) > MAX_CONTENT_LENGTH:
            raise HTTPException(status_code=413, detail="File too large")
        
        with open(filepath, "wb") as f:
            f.write(content)
        
        # Verify and resize image
        try:
            img = Image.open(filepath)
            img.verify()  # Verify it's a valid image
            img = Image.open(filepath)  # Re-open after verify
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid or corrupted image: {str(e)}")
        
        if img.width > paper_width:
            # Calculate new height maintaining aspect ratio
            ratio = paper_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((paper_width, new_height), Image.Resampling.LANCZOS)
            img.save(filepath)
        
        # Print using python-escpos
        p, queue = create_printer(printer)
        
        # Center alignment if requested
        if center:
            p.set(align='center')
        
        # Print image - library handles all conversion with automatic dithering!
        p.image(filepath)
        
        # Reset alignment
        if center:
            p.set(align='left')
        
        # Default one-line feed after image to avoid edge cuts, plus user-configured extra
        p.text('\n')
        if lines_after > 0:
            p.text('\n' * lines_after)
        
        # Cut paper
        if cut:
            p.cut()

        data = collect_output_bytes(p)
        job_id = send_to_cups(queue, data, title="print-image")
        
        return {
            "success": True,
            "message": f"Image printed to {printer}",
            "printer": printer,
            "queue": queue,
            "job_id": job_id,
            "bytes": len(data),
            "filename": filename,
            "lines_after": lines_after
        }
        
    except HTTPException:
        raise
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
    finally:
        # Clean up uploaded file
        try:
            if 'filepath' in locals() and filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


@app.post("/print/qr")
async def print_qr(
    text: str = Query(..., description="Text to encode in QR code"),
    printer: str = Query("printer_1", description="Printer name"),
    size: int = Query(3, description="QR code size (1-8)"),
    lines_after: int = Query(0, description="Feed lines before cut"),
    cut: bool = Query(True, description="Auto cut after printing"),
    center: bool = Query(True, description="Center QR code")
):
    """
    Print QR code to thermal printer
    
    Example: /print/qr?text=https://example.com&printer=printer_1
    """
    # Validate input
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="QR text cannot be empty")
    
    # Clamp values
    size = max(1, min(8, size))
    lines_after = max(0, min(255, lines_after))
    
    try:
        p, queue = create_printer(printer)
        
        # Center alignment if requested
        if center:
            p.set(align='center')
        
        # Print QR code
        p.qr(text, size=size)
        
        # Reset alignment
        if center:
            p.set(align='left')
        
        # Feed lines before cutting
        if lines_after > 0:
            p.text('\n' * lines_after)
        
        # Cut paper
        if cut:
            p.cut()

        data = collect_output_bytes(p)
        job_id = send_to_cups(queue, data, title="print-qr")
        
        return {
            "success": True,
            "message": f"QR code printed to {printer}",
            "printer": printer,
            "queue": queue,
            "job_id": job_id,
            "bytes": len(data),
            "text": text,
            "size": size
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")


@app.post("/print/barcode")
async def print_barcode(
    code: str = Query(..., description="Barcode data"),
    printer: str = Query("printer_1", description="Printer name"),
    barcode_type: str = Query("CODE39", description="Barcode type (EAN13, CODE39, etc)"),
    height: int = Query(64, description="Barcode height"),
    width: int = Query(2, description="Barcode width"),
    lines_after: int = Query(0, description="Feed lines before cut"),
    cut: bool = Query(True, description="Auto cut after printing"),
    center: bool = Query(True, description="Center barcode")
):
    """
    Print barcode to thermal printer
    
    Example: /print/barcode?code=123456789012&barcode_type=EAN13&printer=printer_1
    """
    # Validate input
    if not code or not code.strip():
        raise HTTPException(status_code=400, detail="Barcode data cannot be empty")
    
    # Clamp values
    height = max(1, min(255, height))
    width = max(1, min(6, width))
    lines_after = max(0, min(255, lines_after))
    
    # Validate barcode type
    valid_types = ['UPC-A', 'UPC-E', 'EAN13', 'EAN8', 'CODE39', 'ITF', 'NW7', 'CODABAR', 'CODE93', 'CODE128']
    if barcode_type.upper() not in [t.upper() for t in valid_types]:
        barcode_type = 'CODE39'  # Default fallback
    
    try:
        p, queue = create_printer(printer)
        
        # Center alignment if requested
        if center:
            p.set(align='center')
        
        # Print barcode
        p.barcode(code, barcode_type, height=height, width=width, pos='BELOW', font='A')
        
        # Reset alignment
        if center:
            p.set(align='left')
        
        # Feed lines before cutting
        if lines_after > 0:
            p.text('\n' * lines_after)
        
        # Cut paper
        if cut:
            p.cut()

        data = collect_output_bytes(p)
        job_id = send_to_cups(queue, data, title="print-barcode")
        
        return {
            "success": True,
            "message": f"Barcode printed to {printer}",
            "printer": printer,
            "queue": queue,
            "job_id": job_id,
            "bytes": len(data),
            "code": code,
            "type": barcode_type
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")


@app.api_route("/cut", methods=["GET", "POST"])
async def cut_paper(
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    lines_before: int = Query(0, description="Feed lines before cut"),
    feed: int = Query(None, description="Feed lines (backward compatibility)"),
    mode: str = Query("partial", description="Cut mode (backward compatibility)")
):
    """
    Cut paper with optional feed
    
    Supports both /cut?printer=X and /cut?printer_name=X
    Example: /cut?printer=printer_1&lines_before=5
    """
    # Support both 'printer' and 'printer_name' for backward compatibility
    if printer_name:
        printer = printer_name
    
    # Support both 'feed' and 'lines_before' parameters
    if feed is not None:
        lines_before = feed
    
    # Clamp value
    lines_before = max(0, min(255, lines_before))
    
    try:
        p, queue = create_printer(printer)
        
        # Feed lines before cutting
        if lines_before > 0:
            p.text('\n' * lines_before)
        
        # Cut paper
        p.cut()

        data = collect_output_bytes(p)
        job_id = send_to_cups(queue, data, title="cut")
        
        return {
            "success": True,
            "message": f"Paper cut on {printer}",
            "printer": printer,
            "queue": queue,
            "job_id": job_id,
            "bytes": len(data),
            "lines_before": lines_before
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")


@app.get("/beep")
@app.post("/beep")
async def beep(
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    count: int = Query(1, description="Number of beeps (1-9)"),
    duration: int = Query(1, description="Beep duration units (1-9, each ~100ms)"),
    beep_time: int = Query(None, alias="time", description="Beep duration (backward compatibility)")
):
    """
    Make printer beep
    
    Supports both GET and POST
    Example: /beep?printer=printer_1&count=3&duration=2
    """
    # Support both 'printer' and 'printer_name' for backward compatibility
    if printer_name:
        printer = printer_name
    
    # Support both 'time' and 'duration' parameters
    if beep_time is not None:
        duration = beep_time
    
    try:
        p, queue = create_printer(printer)
        
        # Buzzer command: ESC (B n t - n=number of times, t=duration (1-9, each unit ~100ms)
        count = max(1, min(9, count))
        duration = max(1, min(9, duration))
        
        # Send buzzer command directly
        p._raw(b'\x1b\x42' + bytes([count, duration]))

        data = collect_output_bytes(p)
        job_id = send_to_cups(queue, data, title="beep")
        
        return {
            "success": True,
            "message": f"Beep sent to {printer}",
            "printer": printer,
            "queue": queue,
            "job_id": job_id,
            "bytes": len(data),
            "count": count,
            "duration_units_100ms": duration
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")


@app.post("/print-raw")
async def print_raw(
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    base64_data: str = Query(None, alias="base64", description="Base64 encoded ESC/POS data"),
    hex_data: str = Query(None, alias="hex", description="Hex encoded ESC/POS data")
):
    """
    Send raw ESC/POS commands to printer
    
    Example: /print-raw?printer=printer_1&base64=G0BA
    """
    # Support both 'printer' and 'printer_name' for backward compatibility
    if printer_name:
        printer = printer_name
    
    if not base64_data and not hex_data:
        raise HTTPException(status_code=400, detail="Provide 'base64' or 'hex' parameter")
    
    try:
        # Decode data
        if base64_data:
            # Validate base64 length (prevent huge payloads)
            if len(base64_data) > MAX_CONTENT_LENGTH * 2:  # base64 is ~1.33x larger
                raise HTTPException(status_code=413, detail="Raw data too large")
            data = base64.b64decode(base64_data)
        else:
            if len(hex_data) > MAX_CONTENT_LENGTH * 2:
                raise HTTPException(status_code=413, detail="Raw data too large")
            data = binascii.unhexlify(hex_data.strip())
        
        if len(data) == 0:
            raise HTTPException(status_code=400, detail="Empty data provided")
        
        if len(data) > MAX_CONTENT_LENGTH:
            raise HTTPException(status_code=413, detail="Raw data too large")
        
        queue = get_printer_queue(printer)
        job_id = send_to_cups(queue, data, title="print-raw")
        
        return {
            "success": True,
            "message": f"Raw data sent to {printer}",
            "printer": printer,
            "queue": queue,
            "job_id": job_id,
            "bytes": len(data)
        }
    
    except HTTPException:
        raise
    except binascii.Error as e:
        raise HTTPException(status_code=400, detail=f"Invalid hex encoding: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid data encoding: {str(e)}")


@app.api_route("/drawer", methods=["GET", "POST"])
async def drawer(
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    pin: int = Query(0, description="Pin number (0 or 1)"),
    t1: int = Query(100, description="ON time (0-255)"),
    t2: int = Query(100, description="OFF time (0-255)")
):
    """
    Open cash drawer
    
    Sends pulse to cash drawer on pin 2 or pin 5
    Example: /drawer?printer=printer_1&pin=0&t1=100&t2=100
    """
    # Support both 'printer' and 'printer_name' for backward compatibility
    if printer_name:
        printer = printer_name
    
    try:
        p, queue = create_printer(printer)
        
        # Clamp values
        pin_val = 0 if pin == 0 else 1
        t1_val = max(0, min(255, t1))
        t2_val = max(0, min(255, t2))
        
        # ESC p m t1 t2 - Cash drawer kick command
        # m: 0 (pin 2) or 1 (pin 5)
        p._raw(b'\x1b\x70' + bytes([pin_val, t1_val, t2_val]))

        data = collect_output_bytes(p)
        job_id = send_to_cups(queue, data, title="drawer")
        
        return {
            "success": True,
            "message": f"Cash drawer pulse sent to {printer}",
            "printer": printer,
            "queue": queue,
            "job_id": job_id,
            "bytes": len(data),
            "pin": pin_val,
            "t1": t1_val,
            "t2": t2_val
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")


@app.api_route("/feed", methods=["GET", "POST"])
async def feed(
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    lines: int = Query(3, description="Number of lines to feed (0-255)")
):
    """
    Feed paper lines
    
    Example: /feed?printer=printer_1&lines=5
    """
    # Support both 'printer' and 'printer_name' for backward compatibility
    if printer_name:
        printer = printer_name
    
    try:
        p, queue = create_printer(printer)
        
        # Clamp value
        lines_val = max(0, min(255, lines))
        
        # ESC d n - Feed n lines
        p._raw(b'\x1b\x64' + bytes([lines_val]))

        data = collect_output_bytes(p)
        job_id = send_to_cups(queue, data, title="feed")
        
        return {
            "success": True,
            "message": f"Fed {lines_val} lines on {printer}",
            "printer": printer,
            "queue": queue,
            "job_id": job_id,
            "bytes": len(data),
            "lines": lines_val
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    print(f"Starting Thermal Printer API (ESC/POS -> CUPS raw) on {SERVER_HOST}:{SERVER_PORT}")
    try:
        print(f"Available printers/queues: {list_cups_printers()}")
    except Exception as e:
        print(f"Could not list CUPS printers: {e}")
    print(f"Features: Text, Images (with auto-dithering), QR codes, Barcodes, Cash drawer, Feed")
    print(f"Spooling via CUPS raw queues using pycups.")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
