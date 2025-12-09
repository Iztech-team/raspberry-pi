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


def scan_network_for_printers():
    """
    Actively scan the network for printers on port 9100.
    This is more reliable than passive mDNS discovery after boot.
    Returns list of discovered socket:// URIs.
    """
    discovered = []
    try:
        # Get local IP and subnet
        local_ip = get_local_ip()
        if not local_ip or local_ip == "Unknown":
            print("  Cannot determine local IP for network scan")
            return discovered
        
        subnet = '.'.join(local_ip.split('.')[:3])
        print(f"  Scanning subnet {subnet}.0/24 for printers on port 9100...")
        
        # Try nmap first (faster and more reliable)
        if subprocess.run(['which', 'nmap'], capture_output=True).returncode == 0:
            print("  Using nmap for fast scanning...")
            result = subprocess.run(
                ['nmap', '-p', '9100', '--open', '-T4', '--host-timeout', '10s', f'{subnet}.0/24'],
                capture_output=True, text=True, timeout=120
            )
            # Parse nmap output for IPs with open port 9100
            current_ip = None
            for line in result.stdout.splitlines():
                if 'Nmap scan report for' in line:
                    # Extract IP from line like "Nmap scan report for 192.168.1.100"
                    parts = line.split()
                    current_ip = parts[-1].strip('()')
                elif '9100/tcp' in line and 'open' in line and current_ip:
                    discovered.append(f"socket://{current_ip}:9100")
                    current_ip = None
        else:
            # Fallback: manual scan with longer timeout
            print("  Using manual scan (nmap not available, this may take a while)...")
            import socket as sock
            for i in range(1, 255):
                ip = f"{subnet}.{i}"
                try:
                    s = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
                    s.settimeout(1)  # 1 second timeout per host
                    if s.connect_ex((ip, 9100)) == 0:
                        discovered.append(f"socket://{ip}:9100")
                        print(f"    Found printer at {ip}:9100")
                    s.close()
                except:
                    pass
        
        print(f"  Network scan found {len(discovered)} printer(s)")
        
    except subprocess.TimeoutExpired:
        print("  Network scan timed out")
    except Exception as e:
        print(f"  Network scan error: {e}")
    
    return discovered


def discover_and_add_printers():
    """
    Discover network printers and add any new ones to CUPS.
    Uses both passive (lpinfo) and active (nmap) discovery.
    Idempotent: skips URIs that are already configured.
    """
    try:
        # Existing printers and URIs
        existing_printers = subprocess.run(
            ['lpstat', '-p'], capture_output=True, text=True
        ).stdout
        existing_names = [
            line.split()[1] for line in existing_printers.splitlines()
            if line.startswith('printer ')
        ]

        existing_uri_output = subprocess.run(
            ['lpstat', '-v'], capture_output=True, text=True
        ).stdout
        existing_uris = set()
        for line in existing_uri_output.splitlines():
            if ': ' in line:
                existing_uris.add(line.split(': ', 1)[1].strip())
        
        print(f"  Currently {len(existing_names)} printer(s) configured")
        
        discovered_uris = set()
        
        # Method 1: Passive discovery via CUPS/lpinfo (mDNS/Bonjour)
        print("  Trying passive discovery (lpinfo)...")
        lpinfo_out = subprocess.run(
            ['lpinfo', '-v'], capture_output=True, text=True
        ).stdout.splitlines()
        for line in lpinfo_out:
            if 'socket://' in line.lower():
                parts = line.split(None, 1)
                if len(parts) >= 2:
                    discovered_uris.add(parts[1].strip())
        
        # Method 2: Active network scan (more reliable after boot)
        print("  Trying active network scan...")
        network_uris = scan_network_for_printers()
        discovered_uris.update(network_uris)
        
        print(f"  Total discovered URIs: {len(discovered_uris)}")

        # Find next printer_N index
        next_idx = 1
        numbered = [n for n in existing_names if n.startswith('printer_')]
        if numbered:
            try:
                next_idx = max(int(n.split('_')[1]) for n in numbered) + 1
            except Exception:
                pass

        new_printers = []
        for uri in discovered_uris:
            if uri in existing_uris:
                print(f"    Skipping {uri} (already configured)")
                continue
            name = f"printer_{next_idx}"
            next_idx += 1
            print(f"  Adding new printer {name} -> {uri}")
            add_cmd = ['lpadmin', '-p', name, '-v', uri, '-E']
            result = subprocess.run(add_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                new_printers.append((name, uri))
                print(f"    ✓ Added {name}")
            else:
                print(f"    ✗ Failed to add {name}: {result.stderr.strip()}")

        if new_printers:
            print(f"Discovered and added {len(new_printers)} new printer(s).")
        else:
            print("No new printers discovered.")

    except Exception as e:
        print(f"Printer discovery error: {e}")


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
    """Generate a boot notification receipt image - matches test print format"""
    
    # Create image (576px width for 80mm thermal printer)
    width = 576
    height = 1600
    img = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(img)
    
    # Load fonts
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        header_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        normal_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except:
        title_font = header_font = normal_font = small_font = ImageFont.load_default()
    
    y = 20
    padding = 20
    
    # Try to load company logo
    if logo_path and os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path)
            logo_max_width = 400
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
            y += logo.height + 20
        except:
            pass
    
    # STATUS MESSAGE AT TOP (unique to boot notification)
    status_msg = "I'm up and ready"
    bbox = draw.textbbox((0, 0), status_msg, font=title_font)
    msg_width = bbox[2] - bbox[0]
    draw.text(((width - msg_width) // 2, y), status_msg, fill='black', font=title_font)
    y += 35
    
    status_msg2 = "to get back to work!"
    bbox = draw.textbbox((0, 0), status_msg2, font=title_font)
    msg_width = bbox[2] - bbox[0]
    draw.text(((width - msg_width) // 2, y), status_msg2, fill='black', font=title_font)
    y += 50
    
    # Draw line
    draw.line([(padding, y), (width - padding, y)], fill='black', width=2)
    y += 20
    
    # SERVER INFORMATION Section (same as test print)
    draw.text((padding, y), "SERVER INFORMATION", fill='black', font=header_font)
    y += 30
    
    info_lines = [
        ("Server IP:", server_info['local_ip']),
        ("Server Port:", server_info.get('port', '3006')),
        ("Hostname:", server_info['hostname']),
    ]
    
    # Add Tailscale IP if available
    if server_info.get('tailscale_ip'):
        info_lines.append(("Tailscale IP:", server_info['tailscale_ip']))
    
    info_lines.append(("Local URL:", f"http://{server_info['local_ip']}:{server_info.get('port', '3006')}"))
    
    # Add Tailscale URL if available
    if server_info.get('tailscale_ip'):
        info_lines.append(("Remote URL:", f"http://{server_info['tailscale_ip']}:{server_info.get('port', '3006')}"))
    
    info_lines.append(("Installation:", server_info.get('install_dir', '/home/pi/printer-server')))
    
    for label, value in info_lines:
        draw.text((padding, y), label, fill='black', font=normal_font)
        draw.text((padding + 150, y), str(value), fill='black', font=normal_font)
        y += 25
    
    y += 10
    draw.line([(padding, y), (width - padding, y)], fill='black', width=2)
    y += 20
    
    # PRINTER INFORMATION Section (same as test print)
    draw.text((padding, y), "PRINTER INFORMATION", fill='black', font=header_font)
    y += 30
    
    # Extract IP and port from URI
    printer_ip = "unknown"
    printer_port = "9100"
    uri = printer_info.get('uri', '')
    if '://' in uri:
        try:
            # Parse socket://192.168.1.100:9100
            addr_part = uri.split('://')[1]
            if ':' in addr_part:
                printer_ip = addr_part.split(':')[0]
                printer_port = addr_part.split(':')[1]
            else:
                printer_ip = addr_part
        except:
            pass
    
    printer_lines = [
        ("Printer IP:", printer_ip),
        ("Printer Port:", printer_port),
        ("Printer Name:", printer_info['name']),
    ]
    
    for label, value in printer_lines:
        draw.text((padding, y), label, fill='black', font=normal_font)
        draw.text((padding + 150, y), str(value), fill='black', font=normal_font)
        y += 25
    
    y += 10
    draw.line([(padding, y), (width - padding, y)], fill='black', width=2)
    y += 20
    
    # Timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bbox = draw.textbbox((0, 0), timestamp, font=small_font)
    ts_width = bbox[2] - bbox[0]
    draw.text(((width - ts_width) // 2, y), timestamp, fill='black', font=small_font)
    y += 30
    
    # Closing message
    y += 10
    draw.line([(padding, y), (width - padding, y)], fill='black', width=2)
    y += 25
    
    # Success message (same as test print)
    success_message = "System recovered successfully!"
    bbox = draw.textbbox((0, 0), success_message, font=normal_font)
    line_width = bbox[2] - bbox[0]
    draw.text(((width - line_width) // 2, y), success_message, fill='black', font=normal_font)
    y += 25
    
    ready_message = "Ready to receive print jobs."
    bbox = draw.textbbox((0, 0), ready_message, font=normal_font)
    line_width = bbox[2] - bbox[0]
    draw.text(((width - line_width) // 2, y), ready_message, fill='black', font=normal_font)
    y += 30
    
    # Crop to actual content height
    img = img.crop((0, 0, width, y + 30))
    
    return img


def print_receipt(printer_name, image_path, script_dir):
    """Print the receipt image to a specific printer using python-escpos (fast C-optimized)"""
    try:
        # Use python-escpos directly - much faster than print_image_any.py
        # python-escpos uses PIL's C-optimized dithering
        from escpos.printer import Dummy
        from PIL import Image
        import tempfile
        
        # Create dummy printer to capture ESC/POS output
        p = Dummy()
        
        # Center align
        p.set(align='center')
        
        # Print image - python-escpos uses fast C-optimized PIL dithering
        p.image(image_path)
        
        # Reset alignment
        p.set(align='left')
        
        # Feed lines before cut
        p.text('\n\n\n')
        
        # Cut paper
        p.cut()
        
        # Beep (3 beeps, 500ms each)
        p._raw(b'\x1b\x42\x03\x05')
        
        # Write ESC/POS data to temp file and print via lp
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as tmp:
            tmp.write(p.output)
            tmp_path = tmp.name
        
        try:
            print_cmd = ['lp', '-d', printer_name, '-o', 'raw', tmp_path]
            result = subprocess.run(print_cmd, capture_output=True, timeout=30)
            
            if result.returncode == 0:
                return True
            else:
                print(f"  Print command failed: {result.stderr.decode()}")
                return False
        finally:
            # Clean up temp file
            try:
                os.remove(tmp_path)
            except:
                pass
            
    except subprocess.TimeoutExpired:
        print(f"  Print timeout for {printer_name}")
        return False
    except ImportError:
        print(f"  Warning: python-escpos not installed, falling back to print_image_any.py")
        return print_receipt_fallback(printer_name, image_path, script_dir)
    except Exception as e:
        print(f"  Error printing to {printer_name}: {e}")
        return False


def print_receipt_fallback(printer_name, image_path, script_dir):
    """Fallback to print_image_any.py if python-escpos is not available"""
    try:
        print_script = os.path.join(script_dir, 'print_image_any.py')
        
        if not os.path.exists(print_script):
            print(f"  Warning: print_image_any.py not found at {print_script}")
            return False
        
        # Convert image to ESC/POS and pipe to lp
        convert_cmd = [
            sys.executable, print_script, image_path,
            '--max-width', '576',
            '--mode', 'gsv0',
            '--align', 'center',
            '--no-dither'  # Skip dithering for speed
        ]
        
        print_cmd = ['lp', '-d', printer_name, '-o', 'raw']
        
        # Run conversion and pipe to print
        convert_proc = subprocess.Popen(convert_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print_proc = subprocess.Popen(print_cmd, stdin=convert_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        convert_proc.stdout.close()
        
        stdout, stderr = print_proc.communicate(timeout=30)
        
        if print_proc.returncode == 0:
            # Send cut command
            time.sleep(0.5)
            cut_cmd = ['lp', '-d', printer_name, '-o', 'raw']
            cut_proc = subprocess.Popen(cut_cmd, stdin=subprocess.PIPE)
            cut_proc.communicate(input=b'\x1b\x64\x03\x1d\x56\x01', timeout=5)
            
            # Send beep command (3 beeps)
            time.sleep(0.5)
            beep_cmd = ['lp', '-d', printer_name, '-o', 'raw']
            beep_proc = subprocess.Popen(beep_cmd, stdin=subprocess.PIPE)
            beep_proc.communicate(input=b'\x1b\x42\x03\x05', timeout=5)
            
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


def wait_for_cups():
    """Wait for CUPS service to be ready - keeps trying until success"""
    print("Waiting for CUPS service...")
    start_time = time.time()
    attempt = 0
    
    while True:  # Keep trying forever until CUPS is ready
        attempt += 1
        try:
            result = subprocess.run(['lpstat', '-r'], capture_output=True, text=True, timeout=5)
            if 'scheduler is running' in result.stdout.lower():
                elapsed = int(time.time() - start_time)
                print(f"CUPS is ready! (took {elapsed} seconds, {attempt} attempts)")
                return True
        except:
            pass
        
        # Show progress every 30 seconds
        elapsed = int(time.time() - start_time)
        if elapsed > 0 and elapsed % 30 == 0:
            print(f"  Still waiting for CUPS... ({elapsed}s, attempt #{attempt})")
        
        time.sleep(5)


def wait_for_network():
    """Wait for network to be ready - keeps trying until success"""
    print("Waiting for network...")
    start_time = time.time()
    attempt = 0
    
    while True:  # Keep trying forever until network is ready
        attempt += 1
        ip = get_local_ip()
        if ip and ip != "Unknown":
            elapsed = int(time.time() - start_time)
            print(f"Network ready! IP: {ip} (took {elapsed} seconds, {attempt} attempts)")
            return True
        
        # Show progress every 30 seconds
        elapsed = int(time.time() - start_time)
        if elapsed > 0 and elapsed % 30 == 0:
            print(f"  Still waiting for network... ({elapsed}s, attempt #{attempt})")
        
        time.sleep(5)


def main():
    """Main function to run boot notifications"""
    print("=" * 50)
    print("Printer Boot Notification Service")
    print("=" * 50)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Determine script directory (for finding logo and fallback scripts)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Also check the installation directory
    install_dir = os.path.expanduser('~/printer-server')
    if os.path.exists(install_dir):
        script_dir = install_dir
    
    # Wait for services to be ready (keeps trying until success)
    wait_for_network()
    wait_for_cups()
    
    # Wait for network printers to boot up after power outage
    # Printers typically take 30-60 seconds to fully boot and become network-accessible
    printer_boot_delay = int(os.environ.get('PRINTER_BOOT_DELAY', '30'))
    print(f"\nWaiting {printer_boot_delay}s for network printers to boot up...")
    print("  (Set PRINTER_BOOT_DELAY env var to adjust)")
    time.sleep(printer_boot_delay)

    # Discover and add new printers (uses both passive and active scanning)
    print("\nDiscovering and adding new printers...")
    discover_and_add_printers()
    
    # Give a bit more time for everything to stabilize
    print("Waiting 5 seconds for system to stabilize...")
    time.sleep(5)
    
    # Gather server information
    server_info = {
        'hostname': get_hostname(),
        'local_ip': get_local_ip(),
        'tailscale_ip': get_tailscale_ip(),
        'port': os.environ.get('SERVER_PORT', '3006'),
        'uptime': get_system_uptime(),
        'install_dir': script_dir
    }
    
    print(f"\nServer Info:")
    print(f"  Hostname: {server_info['hostname']}")
    print(f"  Local IP: {server_info['local_ip']}")
    print(f"  Tailscale IP: {server_info['tailscale_ip'] or 'Not connected'}")
    print(f"  Uptime: {server_info['uptime']}")
    print(f"  Install Dir: {server_info['install_dir']}")
    
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
    
    max_retries = int(os.environ.get('PRINT_MAX_RETRIES', '3'))
    retry_delay = int(os.environ.get('PRINT_RETRY_DELAY', '10'))
    
    for printer in printers:
        print(f"\nProcessing: {printer['name']}")
        
        try:
            # Generate receipt image
            img = generate_boot_receipt(printer, server_info, logo_path)
            
            # Save to temp file
            temp_image = f"/tmp/boot_notify_{printer['name']}_{int(time.time())}.png"
            img.save(temp_image)
            print(f"  Generated receipt image: {temp_image}")
            
            # Print with retry logic (printers may still be booting)
            printed = False
            for attempt in range(1, max_retries + 1):
                print(f"  Attempt {attempt}/{max_retries}...")
                if print_receipt(printer['name'], temp_image, script_dir):
                    print(f"  ✓ SUCCESS: Boot notification sent to {printer['name']}")
                    success_count += 1
                    printed = True
                    break
                else:
                    if attempt < max_retries:
                        print(f"  ⚠ Failed, retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
            
            if not printed:
                print(f"  ✗ FAILED: Could not send to {printer['name']} after {max_retries} attempts")
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

