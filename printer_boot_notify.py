#!/usr/bin/env python3
"""
Printer Boot Notification Script
Prints a status receipt to all configured printers when the system boots up.
This helps identify which printers are back online after a power outage.
"""

import subprocess
import sys
import os
import socket
import time
from datetime import datetime
from pathlib import Path

# Try to import PIL, exit gracefully if not available
try:
    from PIL import Image, ImageDraw, ImageFont
    import textwrap
except ImportError:
    print("PIL not installed. Run: pip install Pillow")
    sys.exit(1)


def get_hostname():
    """Get the system hostname"""
    return socket.gethostname()


def get_local_ip():
    """Get the local IP address"""
    try:
        # Get all IPs
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
        ips = result.stdout.strip().split()
        
        # Prioritize private IPs
        for ip in ips:
            if ip.startswith('192.168.') or ip.startswith('10.') or ip.startswith('172.'):
                return ip
        
        return ips[0] if ips else "Unknown"
    except:
        return "Unknown"


def get_tailscale_ip():
    """Get Tailscale IP if connected"""
    try:
        result = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except:
        pass
    return None


def get_configured_printers():
    """Get list of configured printers from CUPS"""
    try:
        result = subprocess.run(['lpstat', '-p'], capture_output=True, text=True)
        if result.returncode != 0:
            return []
        
        printers = []
        for line in result.stdout.strip().split('\n'):
            if line.startswith('printer '):
                parts = line.split()
                if len(parts) >= 2:
                    printer_name = parts[1]
                    # Get printer URI
                    uri_result = subprocess.run(['lpstat', '-v', printer_name], capture_output=True, text=True)
                    uri = "unknown"
                    if uri_result.returncode == 0:
                        # Parse: "device for printer_1: socket://192.168.1.100:9100"
                        uri_line = uri_result.stdout.strip()
                        if ': ' in uri_line:
                            uri = uri_line.split(': ', 1)[1]
                    
                    # Check if printer is enabled
                    is_enabled = 'enabled' in line.lower() or 'idle' in line.lower()
                    
                    printers.append({
                        'name': printer_name,
                        'uri': uri,
                        'enabled': is_enabled
                    })
        
        return printers
    except Exception as e:
        print(f"Error getting printers: {e}")
        return []


def get_system_uptime():
    """Get system uptime"""
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.read().split()[0])
            
            if uptime_seconds < 60:
                return f"{int(uptime_seconds)} seconds"
            elif uptime_seconds < 3600:
                return f"{int(uptime_seconds / 60)} minutes"
            else:
                hours = int(uptime_seconds / 3600)
                minutes = int((uptime_seconds % 3600) / 60)
                return f"{hours}h {minutes}m"
    except:
        return "Unknown"


def generate_boot_receipt(printer_info, server_info, logo_path=None):
    """Generate a boot notification receipt image"""
    
    # Create image (576px width for 80mm thermal printer)
    width = 576
    height = 1400
    img = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(img)
    
    # Load fonts
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        header_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        normal_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        emoji_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    except:
        title_font = header_font = normal_font = small_font = emoji_font = ImageFont.load_default()
    
    y = 20
    padding = 20
    
    # Try to load company logo
    if logo_path and os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path)
            logo_max_width = 350
            if logo.width > logo_max_width:
                ratio = logo_max_width / logo.width
                new_height = int(logo.height * ratio)
                logo = logo.resize((logo_max_width, new_height), Image.Resampling.LANCZOS)
            
            logo_x = (width - logo.width) // 2
            
            if logo.mode == 'RGBA':
                logo_bg = Image.new('RGB', logo.size, 'white')
                logo_bg.paste(logo, (0, 0), logo)
                logo = logo_bg
            elif logo.mode != 'RGB':
                logo = logo.convert('RGB')
            
            img.paste(logo, (logo_x, y))
            y += logo.height + 15
        except:
            pass
    
    # Status indicator (checkmark)
    status_text = "ONLINE"
    bbox = draw.textbbox((0, 0), status_text, font=title_font)
    status_width = bbox[2] - bbox[0]
    draw.text(((width - status_width) // 2, y), status_text, fill='black', font=title_font)
    y += 45
    
    # Main message
    main_msg = "I'm up and ready"
    bbox = draw.textbbox((0, 0), main_msg, font=header_font)
    msg_width = bbox[2] - bbox[0]
    draw.text(((width - msg_width) // 2, y), main_msg, fill='black', font=header_font)
    y += 30
    
    main_msg2 = "to get back to work!"
    bbox = draw.textbbox((0, 0), main_msg2, font=header_font)
    msg_width = bbox[2] - bbox[0]
    draw.text(((width - msg_width) // 2, y), main_msg2, fill='black', font=header_font)
    y += 45
    
    # Divider line
    draw.line([(padding, y), (width - padding, y)], fill='black', width=2)
    y += 20
    
    # Printer Information Section
    draw.text((padding, y), "PRINTER INFO", fill='black', font=header_font)
    y += 30
    
    printer_lines = [
        ("Printer Name:", printer_info['name']),
        ("Printer URI:", ""),
    ]
    
    for label, value in printer_lines:
        draw.text((padding, y), label, fill='black', font=normal_font)
        if value:
            draw.text((padding + 140, y), value, fill='black', font=normal_font)
        y += 25
    
    # URI on separate line (might be long)
    uri_text = printer_info['uri']
    if len(uri_text) > 35:
        wrapped_uri = textwrap.fill(uri_text, width=40)
        for line in wrapped_uri.split('\n'):
            draw.text((padding + 15, y), line, fill='black', font=small_font)
            y += 20
    else:
        draw.text((padding + 15, y), uri_text, fill='black', font=small_font)
        y += 25
    
    y += 10
    draw.line([(padding, y), (width - padding, y)], fill='black', width=2)
    y += 20
    
    # Server Information Section
    draw.text((padding, y), "SERVER INFO", fill='black', font=header_font)
    y += 30
    
    server_lines = [
        ("Hostname:", server_info['hostname']),
        ("Local IP:", server_info['local_ip']),
        ("Server Port:", server_info.get('port', '3006')),
    ]
    
    if server_info.get('tailscale_ip'):
        server_lines.append(("Tailscale IP:", server_info['tailscale_ip']))
    
    server_lines.append(("Uptime:", server_info['uptime']))
    
    for label, value in server_lines:
        draw.text((padding, y), label, fill='black', font=normal_font)
        draw.text((padding + 140, y), str(value), fill='black', font=normal_font)
        y += 25
    
    y += 10
    draw.line([(padding, y), (width - padding, y)], fill='black', width=2)
    y += 20
    
    # Access URLs
    draw.text((padding, y), "ACCESS URLS", fill='black', font=header_font)
    y += 30
    
    local_url = f"http://{server_info['local_ip']}:{server_info.get('port', '3006')}"
    draw.text((padding, y), "Local:", fill='black', font=normal_font)
    y += 22
    draw.text((padding + 15, y), local_url, fill='black', font=small_font)
    y += 25
    
    if server_info.get('tailscale_ip'):
        remote_url = f"http://{server_info['tailscale_ip']}:{server_info.get('port', '3006')}"
        draw.text((padding, y), "Remote:", fill='black', font=normal_font)
        y += 22
        draw.text((padding + 15, y), remote_url, fill='black', font=small_font)
        y += 25
    
    y += 10
    draw.line([(padding, y), (width - padding, y)], fill='black', width=2)
    y += 25
    
    # Boot timestamp
    boot_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    draw.text((padding, y), "Boot Time:", fill='black', font=normal_font)
    draw.text((padding + 140, y), boot_time, fill='black', font=normal_font)
    y += 35
    
    # Closing message
    draw.line([(padding, y), (width - padding, y)], fill='black', width=2)
    y += 25
    
    closing = "System recovered successfully!"
    bbox = draw.textbbox((0, 0), closing, font=normal_font)
    closing_width = bbox[2] - bbox[0]
    draw.text(((width - closing_width) // 2, y), closing, fill='black', font=normal_font)
    y += 30
    
    closing2 = "Ready to receive print jobs."
    bbox = draw.textbbox((0, 0), closing2, font=small_font)
    closing2_width = bbox[2] - bbox[0]
    draw.text(((width - closing2_width) // 2, y), closing2, fill='black', font=small_font)
    y += 35
    
    # Crop to actual content
    img = img.crop((0, 0, width, y + 20))
    
    return img


def print_receipt(printer_name, image_path, script_dir):
    """Print the receipt image to a specific printer"""
    try:
        # Use print_image_any.py to convert and print
        print_script = os.path.join(script_dir, 'print_image_any.py')
        
        if not os.path.exists(print_script):
            print(f"  Warning: print_image_any.py not found at {print_script}")
            return False
        
        # Convert image to ESC/POS and pipe to lp
        convert_cmd = [
            sys.executable, print_script, image_path,
            '--max-width', '576',
            '--mode', 'gsv0',
            '--align', 'center'
        ]
        
        print_cmd = ['lp', '-d', printer_name, '-o', 'raw']
        
        # Run conversion and pipe to print
        convert_proc = subprocess.Popen(convert_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print_proc = subprocess.Popen(print_cmd, stdin=convert_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        convert_proc.stdout.close()
        
        stdout, stderr = print_proc.communicate(timeout=30)
        
        if print_proc.returncode == 0:
            # Send beep command (3 beeps)
            time.sleep(0.5)
            beep_cmd = ['lp', '-d', printer_name, '-o', 'raw']
            beep_proc = subprocess.Popen(beep_cmd, stdin=subprocess.PIPE)
            beep_proc.communicate(input=b'\x1b\x42\x03\x05', timeout=5)
            
            # Send cut command
            time.sleep(0.5)
            cut_cmd = ['lp', '-d', printer_name, '-o', 'raw']
            cut_proc = subprocess.Popen(cut_cmd, stdin=subprocess.PIPE)
            cut_proc.communicate(input=b'\x1b\x64\x03\x1d\x56\x01', timeout=5)
            
            return True
        else:
            print(f"  Print command failed: {stderr.decode()}")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"  Print timeout for {printer_name}")
        return False
    except Exception as e:
        print(f"  Error printing to {printer_name}: {e}")
        return False


def wait_for_cups(max_wait=60):
    """Wait for CUPS service to be ready"""
    print("Waiting for CUPS service...")
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        try:
            result = subprocess.run(['lpstat', '-r'], capture_output=True, text=True, timeout=5)
            if 'scheduler is running' in result.stdout.lower():
                print("CUPS is ready!")
                return True
        except:
            pass
        
        time.sleep(2)
    
    print("Warning: CUPS may not be fully ready")
    return False


def wait_for_network(max_wait=60):
    """Wait for network to be ready"""
    print("Waiting for network...")
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        ip = get_local_ip()
        if ip and ip != "Unknown":
            print(f"Network ready! IP: {ip}")
            return True
        time.sleep(2)
    
    print("Warning: Network may not be fully ready")
    return False


def main():
    """Main function to run boot notifications"""
    print("=" * 50)
    print("Printer Boot Notification Service")
    print("=" * 50)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Determine script directory (for finding print_image_any.py and logo)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Also check the installation directory
    install_dir = os.path.expanduser('~/printer-server')
    if os.path.exists(install_dir):
        script_dir = install_dir
    
    # Wait for services to be ready
    wait_for_network(max_wait=60)
    wait_for_cups(max_wait=60)
    
    # Give a bit more time for everything to stabilize
    print("Waiting 5 seconds for system to stabilize...")
    time.sleep(5)
    
    # Gather server information
    server_info = {
        'hostname': get_hostname(),
        'local_ip': get_local_ip(),
        'tailscale_ip': get_tailscale_ip(),
        'port': os.environ.get('SERVER_PORT', '3006'),
        'uptime': get_system_uptime()
    }
    
    print(f"\nServer Info:")
    print(f"  Hostname: {server_info['hostname']}")
    print(f"  Local IP: {server_info['local_ip']}")
    print(f"  Tailscale IP: {server_info['tailscale_ip'] or 'Not connected'}")
    print(f"  Uptime: {server_info['uptime']}")
    
    # Get configured printers
    printers = get_configured_printers()
    
    if not printers:
        print("\nNo printers configured. Exiting.")
        return
    
    print(f"\nFound {len(printers)} configured printer(s):")
    for p in printers:
        print(f"  - {p['name']}: {p['uri']}")
    
    # Find logo file
    logo_path = None
    for possible_logo in ['BarakaOS_Logo.png', 'logo.png']:
        full_path = os.path.join(script_dir, possible_logo)
        if os.path.exists(full_path):
            logo_path = full_path
            break
    
    print(f"\nLogo: {logo_path or 'Not found'}")
    
    # Print to each printer
    print("\n" + "=" * 50)
    print("Sending boot notifications to printers...")
    print("=" * 50)
    
    success_count = 0
    fail_count = 0
    
    for printer in printers:
        print(f"\nProcessing: {printer['name']}")
        
        try:
            # Generate receipt image
            img = generate_boot_receipt(printer, server_info, logo_path)
            
            # Save to temp file
            temp_image = f"/tmp/boot_notify_{printer['name']}_{int(time.time())}.png"
            img.save(temp_image)
            print(f"  Generated receipt image: {temp_image}")
            
            # Print it
            if print_receipt(printer['name'], temp_image, script_dir):
                print(f"  SUCCESS: Boot notification sent to {printer['name']}")
                success_count += 1
            else:
                print(f"  FAILED: Could not send to {printer['name']}")
                fail_count += 1
            
            # Clean up temp file
            try:
                os.remove(temp_image)
            except:
                pass
                
        except Exception as e:
            print(f"  ERROR: {e}")
            fail_count += 1
    
    # Summary
    print("\n" + "=" * 50)
    print("Boot Notification Summary")
    print("=" * 50)
    print(f"  Total Printers: {len(printers)}")
    print(f"  Successful: {success_count}")
    print(f"  Failed: {fail_count}")
    print(f"\nCompleted at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == '__main__':
    main()

