import os
import uuid
import base64
import binascii
from typing import Optional

from fastapi import FastAPI, UploadFile, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# ESC/POS library - NO MORE PPD/CUPS BULLSHIT!
from escpos.printer import Network
from escpos.exceptions import Error as EscposError

# Load environment variables
load_dotenv()

# Configuration
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "bmp", "gif"}
MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_SIZE_MB", "20")) * 1024 * 1024
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "3006"))

# Printer configurations
PRINTERS = {
    "printer_1": {"host": "192.168.1.10", "port": 9100},
    "printer_2": {"host": "192.168.1.105", "port": 9100},
}

# Connection pool - reuse connections instead of creating new ones!
_printer_connections = {}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename: str) -> bool:
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_printer(printer_name: str) -> Network:
    """Get printer connection by name - REUSES connections for speed!"""
    if printer_name not in PRINTERS:
        raise HTTPException(status_code=400, detail=f"Unknown printer: {printer_name}")
    
    # Reuse existing connection if available
    if printer_name in _printer_connections:
        return _printer_connections[printer_name]
    
    # Create new connection and cache it
    config = PRINTERS[printer_name]
    try:
        printer = Network(config["host"], port=config["port"])
        _printer_connections[printer_name] = printer
        return printer
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot connect to {printer_name}: {str(e)}")


@app.get("/")
@app.get("/health")
def health():
    """Health check endpoint (backward compatible)"""
    return {
        "ok": True,
        "status": "running",
        "message": "Thermal Printer API with python-escpos",
        "printers": list(PRINTERS.keys())
    }


@app.get("/printers")
def get_printers():
    """List available printers"""
    return {"printers": list(PRINTERS.keys())}


@app.post("/print-text")
@app.post("/print/text")
async def print_text(
    text: str = Query(..., description="Text to print"),
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    lines_after: int = Query(5, description="Feed lines before cut"),
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
    
    try:
        p = get_printer(printer)
        
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
        
        return {
            "success": True,
            "message": f"Text printed to {printer}",
            "printer": printer,
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
    file: UploadFile,
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    lines_after: int = Query(5, description="Feed lines before cut"),
    cut: bool = Query(True, description="Auto cut after printing"),
    impl: str = Query("bitImageRaster", description="Image implementation (bitImageRaster, bitImageColumn, graphics)"),
    high_density_horizontal: bool = Query(True, description="High density horizontal"),
    high_density_vertical: bool = Query(True, description="High density vertical"),
    center: bool = Query(True, description="Center image")
):
    """
    Print image to thermal printer using python-escpos
    
    Supports both /print-image and /print/image endpoints
    The library handles image conversion automatically with built-in dithering!
    """
    # Support both 'printer' and 'printer_name' for backward compatibility
    if printer_name:
        printer = printer_name
    
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    
    if not allowed_file(file.filename):
        raise HTTPException(status_code=400, detail=f"Invalid file type. Allowed: {ALLOWED_EXTENSIONS}")
    
    # Save uploaded file
    filename = secure_filename(file.filename)
    unique_filename = f"{uuid.uuid4()}_{filename}"
    filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
    
    try:
        content = await file.read()
        if len(content) > MAX_CONTENT_LENGTH:
            raise HTTPException(status_code=413, detail="File too large")
        
        with open(filepath, "wb") as f:
            f.write(content)
        
        # Print using python-escpos
        p = get_printer(printer)
        
        # Center alignment if requested
        if center:
            p.set(align='center')
        
        # Print image - library handles all conversion with automatic dithering!
        p.image(
            filepath,
            impl=impl,
            high_density_horizontal=high_density_horizontal,
            high_density_vertical=high_density_vertical
        )
        
        # Reset alignment
        if center:
            p.set(align='left')
        
        # Feed lines before cutting (PREVENTS MID-IMAGE CUTTING!)
        if lines_after > 0:
            p.text('\n' * lines_after)
        
        # Cut paper
        if cut:
            p.cut()
        
        return {
            "success": True,
            "message": f"Image printed to {printer}",
            "printer": printer,
            "filename": filename,
            "lines_after": lines_after,
            "implementation": impl
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
    finally:
        # Clean up uploaded file
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except:
                pass


@app.post("/print/qr")
async def print_qr(
    text: str = Query(..., description="Text to encode in QR code"),
    printer: str = Query("printer_1", description="Printer name"),
    size: int = Query(3, description="QR code size (1-8)"),
    lines_after: int = Query(5, description="Feed lines before cut"),
    cut: bool = Query(True, description="Auto cut after printing"),
    center: bool = Query(True, description="Center QR code")
):
    """
    Print QR code to thermal printer
    
    Example: /print/qr?text=https://example.com&printer=printer_1
    """
    try:
        p = get_printer(printer)
        
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
        
        return {
            "success": True,
            "message": f"QR code printed to {printer}",
            "printer": printer,
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
    lines_after: int = Query(5, description="Feed lines before cut"),
    cut: bool = Query(True, description="Auto cut after printing"),
    center: bool = Query(True, description="Center barcode")
):
    """
    Print barcode to thermal printer
    
    Example: /print/barcode?code=123456789012&barcode_type=EAN13&printer=printer_1
    """
    try:
        p = get_printer(printer)
        
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
        
        return {
            "success": True,
            "message": f"Barcode printed to {printer}",
            "printer": printer,
            "code": code,
            "type": barcode_type
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")


@app.api_route("/cut", methods=["GET", "POST"])
async def cut_paper(
    printer: str = Query("printer_1", description="Printer name"),
    printer_name: str = Query(None, description="Printer name (backward compatibility)"),
    lines_before: int = Query(5, description="Feed lines before cut"),
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
    
    try:
        p = get_printer(printer)
        
        # Feed lines before cutting
        if lines_before > 0:
            p.text('\n' * lines_before)
        
        # Cut paper
        p.cut()
        
        return {
            "success": True,
            "message": f"Paper cut on {printer}",
            "printer": printer,
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
    time: int = Query(None, description="Beep duration (backward compatibility)")
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
    if time is not None:
        duration = time
    
    try:
        p = get_printer(printer)
        
        # Buzzer command: ESC (B n t - n=number of times, t=duration (1-9, each unit ~100ms)
        count = max(1, min(9, count))
        duration = max(1, min(9, duration))
        
        # Send buzzer command directly
        p._raw(b'\x1b\x42' + bytes([count, duration]))
        
        return {
            "success": True,
            "message": f"Beep sent to {printer}",
            "printer": printer,
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
            data = base64.b64decode(base64_data)
        else:
            data = binascii.unhexlify(hex_data.strip())
        
        # Send raw data
        p = get_printer(printer)
        p._raw(data)
        
        return {
            "success": True,
            "message": f"Raw data sent to {printer}",
            "printer": printer,
            "bytes": len(data)
        }
        
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
        p = get_printer(printer)
        
        # Clamp values
        pin_val = 0 if pin == 0 else 1
        t1_val = max(0, min(255, t1))
        t2_val = max(0, min(255, t2))
        
        # ESC p m t1 t2 - Cash drawer kick command
        # m: 0 (pin 2) or 1 (pin 5)
        p._raw(b'\x1b\x70' + bytes([pin_val, t1_val, t2_val]))
        
        return {
            "success": True,
            "message": f"Cash drawer pulse sent to {printer}",
            "printer": printer,
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
        p = get_printer(printer)
        
        # Clamp value
        lines_val = max(0, min(255, lines))
        
        # ESC d n - Feed n lines
        p._raw(b'\x1b\x64' + bytes([lines_val]))
        
        return {
            "success": True,
            "message": f"Fed {lines_val} lines on {printer}",
            "printer": printer,
            "lines": lines_val
        }
        
    except EscposError as e:
        raise HTTPException(status_code=500, detail=f"Printer error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    print(f"Starting Thermal Printer API with python-escpos on {SERVER_HOST}:{SERVER_PORT}")
    print(f"Available printers: {list(PRINTERS.keys())}")
    print(f"Features: Text, Images (with auto-dithering), QR codes, Barcodes, Cash drawer, Feed")
    print(f"No CUPS, No PPD, Direct network printing!")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
