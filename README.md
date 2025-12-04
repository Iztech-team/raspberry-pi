# Raspberry Pi Thermal Printer Server

A FastAPI-based REST API server for managing ESC/POS thermal printers on Raspberry Pi. This server provides a simple HTTP interface for printing text, images, QR codes, barcodes, and controlling thermal printers remotely.

## 🌟 Features

- 🖨️ **Print Text** - Send formatted text with customizable alignment, size, bold, and underline
- 🖼️ **Print Images** - Print images (PNG, JPG, JPEG, BMP) with automatic conversion and dithering
- 📱 **Print QR Codes** - Generate and print QR codes directly
- 🏷️ **Print Barcodes** - Support for multiple barcode formats (CODE39, EAN13, etc.)
- ✂️ **Paper Control** - Cut paper and feed lines
- 🔔 **Beep Control** - Trigger printer beeper
- 💰 **Cash Drawer** - Open cash drawer connected to printer
- 🔧 **Raw Commands** - Send raw ESC/POS commands via base64 or hex
- 📚 **Auto Documentation** - Interactive API docs via Swagger UI
- 🌐 **CORS Enabled** - Ready for web applications
- ⚙️ **Environment Configuration** - Easy configuration via .env files

## 📋 Requirements

- **Raspberry Pi** (any model with network connectivity)
- **Python 3.8+** (Python 3.11+ recommended)
- **ESC/POS compatible thermal printer** (80mm recommended)
- **Network connection** to printer (via USB-to-Ethernet adapter or network printer)

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/Iztech-team/raspberry-pi.git
cd raspberry-pi
```

### 2. Create Virtual Environment

```bash
# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Edit the `.env` file if needed (default values work for most cases):

```env
SERVER_HOST=0.0.0.0
SERVER_PORT=3006
UPLOAD_FOLDER=uploads
MAX_UPLOAD_SIZE_MB=20
```

### 5. Configure Printers

Edit `server.py` and update the `PRINTERS` configuration with your printer IP addresses:

```python
PRINTERS = {
    "printer_1": {"host": "192.168.1.87", "port": 9100},
    "printer_2": {"host": "192.168.1.105", "port": 9100},
}
```

### 6. Run the Server

**Development Mode:**
```bash
python server.py
```

**Production Mode:**
```bash
uvicorn server:app --host 0.0.0.0 --port 3006 --workers 4
```

**With Auto-Reload (Development):**
```bash
uvicorn server:app --host 0.0.0.0 --port 3006 --reload
```

The server will start on `http://0.0.0.0:3006`

## 📖 API Documentation

Once the server is running, access the interactive API documentation:

- **Swagger UI**: http://your-raspberry-pi-ip:3006/docs
- **ReDoc**: http://your-raspberry-pi-ip:3006/redoc
- **OpenAPI JSON**: http://your-raspberry-pi-ip:3006/openapi.json

## 🔌 API Endpoints

### Health Check

```http
GET /health
```

Returns server status and available printers.

**Response:**
```json
{
  "ok": true,
  "status": "running",
  "message": "Thermal Printer API with python-escpos",
  "printers": ["printer_1", "printer_2"]
}
```

### Print Text

```http
POST /print-text?text=Hello&printer=printer_1&bold=true&width=2
```

**Query Parameters:**
- `text` (required) - Text to print
- `printer` (optional) - Printer name (default: "printer_1")
- `bold` (optional) - Bold text (default: false)
- `underline` (optional) - Underline mode: 0=none, 1=single, 2=double (default: 0)
- `width` (optional) - Width multiplier 1-8 (default: 1)
- `height` (optional) - Height multiplier 1-8 (default: 1)
- `align` (optional) - Alignment: left, center, right (default: "left")
- `lines_after` (optional) - Feed lines before cut (default: 5)
- `cut` (optional) - Auto cut after printing (default: true)

**Example:**
```bash
curl "http://192.168.1.100:3006/print-text?text=Hello%20World&printer=printer_1&bold=true&width=2&align=center"
```

### Print Image

```http
POST /print-image
Content-Type: multipart/form-data
```

**Form Data:**
- `file` (required) - Image file (PNG, JPG, JPEG, BMP)

**Query Parameters:**
- `printer` (optional) - Printer name (default: "printer_1")
- `center` (optional) - Center image (default: true)
- `lines_after` (optional) - Feed lines before cut (default: 5)
- `cut` (optional) - Auto cut after printing (default: true)
- `impl` (optional) - Implementation: bitImageRaster, bitImageColumn, graphics (default: "bitImageRaster")

**Example:**
```bash
curl -X POST "http://192.168.1.100:3006/print-image?printer=printer_1&center=true" \
  -F "file=@logo.png"
```

### Print QR Code

```http
POST /print/qr?text=https://example.com&printer=printer_1&size=6
```

**Query Parameters:**
- `text` (required) - Text to encode in QR code
- `printer` (optional) - Printer name (default: "printer_1")
- `size` (optional) - QR code size 1-8 (default: 3)
- `center` (optional) - Center QR code (default: true)
- `lines_after` (optional) - Feed lines before cut (default: 5)
- `cut` (optional) - Auto cut after printing (default: true)

**Example:**
```bash
curl "http://192.168.1.100:3006/print/qr?text=https://iztech.com&size=6&center=true"
```

### Print Barcode

```http
POST /print/barcode?code=123456789012&barcode_type=EAN13&printer=printer_1
```

**Query Parameters:**
- `code` (required) - Barcode data
- `printer` (optional) - Printer name (default: "printer_1")
- `barcode_type` (optional) - Barcode type: CODE39, EAN13, etc. (default: "CODE39")
- `height` (optional) - Barcode height (default: 64)
- `width` (optional) - Barcode width (default: 2)
- `center` (optional) - Center barcode (default: true)
- `lines_after` (optional) - Feed lines before cut (default: 5)
- `cut` (optional) - Auto cut after printing (default: true)

**Example:**
```bash
curl "http://192.168.1.100:3006/print/barcode?code=123456789012&barcode_type=EAN13"
```

### Cut Paper

```http
GET /cut?printer=printer_1&lines_before=5
```

**Query Parameters:**
- `printer` (optional) - Printer name (default: "printer_1")
- `lines_before` (optional) - Feed lines before cut (default: 5)

**Example:**
```bash
curl "http://192.168.1.100:3006/cut?printer=printer_1&lines_before=5"
```

### Beep

```http
GET /beep?printer=printer_1&count=3&duration=2
```

**Query Parameters:**
- `printer` (optional) - Printer name (default: "printer_1")
- `count` (optional) - Number of beeps 1-9 (default: 1)
- `duration` (optional) - Beep duration units 1-9, each ~100ms (default: 1)

**Example:**
```bash
curl "http://192.168.1.100:3006/beep?count=3&duration=2"
```

### Open Cash Drawer

```http
GET /drawer?printer=printer_1&pin=0
```

**Query Parameters:**
- `printer` (optional) - Printer name (default: "printer_1")
- `pin` (optional) - Pin number: 0 or 1 (default: 0)
- `t1` (optional) - ON time 0-255 (default: 100)
- `t2` (optional) - OFF time 0-255 (default: 100)

**Example:**
```bash
curl "http://192.168.1.100:3006/drawer?pin=0&t1=100&t2=100"
```

### Feed Paper

```http
GET /feed?printer=printer_1&lines=5
```

**Query Parameters:**
- `printer` (optional) - Printer name (default: "printer_1")
- `lines` (optional) - Number of lines to feed 0-255 (default: 3)

**Example:**
```bash
curl "http://192.168.1.100:3006/feed?lines=5"
```

### Print Raw ESC/POS Commands

```http
POST /print-raw?printer=printer_1&base64=G0BA
```

**Query Parameters:**
- `printer` (optional) - Printer name (default: "printer_1")
- `base64` (optional) - Base64 encoded ESC/POS data
- `hex` (optional) - Hex encoded ESC/POS data

**Example:**
```bash
# Send raw command (cut paper): ESC i
curl "http://192.168.1.100:3006/print-raw?base64=G0Bp"
```

## 🔧 Configuration

### Printer Configuration

Edit the `PRINTERS` dictionary in `server.py`:

```python
PRINTERS = {
    "printer_1": {"host": "192.168.1.87", "port": 9100},
    "printer_2": {"host": "192.168.1.105", "port": 9100},
    "kitchen": {"host": "192.168.1.200", "port": 9100},
}
```

### Environment Variables

Create a `.env` file:

```env
# Server Settings
SERVER_HOST=0.0.0.0
SERVER_PORT=3006

# File Upload Settings
UPLOAD_FOLDER=uploads
MAX_UPLOAD_SIZE_MB=20
```

## 🐛 Troubleshooting

### Printer Not Responding

1. Check printer IP address and port in `PRINTERS` configuration
2. Verify printer is on the same network as Raspberry Pi
3. Test connection: `ping <printer-ip>`
4. Ensure printer port 9100 is accessible

### Permission Denied

```bash
# Run with sudo if needed
sudo python server.py
```

### Module Not Found

```bash
# Ensure virtual environment is activated
source venv/bin/activate

# Reinstall dependencies
pip install -r requirements.txt
```

### Image Not Printing

1. Verify image format is supported (PNG, JPG, JPEG, BMP)
2. Check image size (should be appropriate for thermal printer width)
3. Try different `impl` parameter: `bitImageRaster`, `bitImageColumn`, or `graphics`

## 🔒 Security Considerations

- The server runs with CORS enabled (`*`). For production, restrict to specific origins.
- Consider adding authentication for production deployments.
- Use HTTPS for production environments.
- Keep the `.env` file secure and never commit it to version control.

## 📝 Development

### Running Tests

```bash
# TODO: Add tests
pytest
```

### Code Style

```bash
# Format code
black server.py

# Lint code
flake8 server.py
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

This project is proprietary software owned by Iztech Team.

## 🙏 Acknowledgments

- Built with [FastAPI](https://fastapi.tiangolo.com/)
- Uses [python-escpos](https://python-escpos.readthedocs.io/) for printer communication
- Designed for Raspberry Pi deployment

## 📧 Support

For issues and questions, please contact the Iztech Team or create an issue in this repository.

---

**Made with ❤️ by Iztech Team**
