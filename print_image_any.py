#!/usr/bin/env python3
"""
Image to ESC/POS converter for thermal printers
Converts images to ESC/POS bitmap format with dithering
"""

import sys
import argparse
from PIL import Image
import numpy as np
import io


def floyd_steinberg_dithering(img_array):
    """
    Apply Floyd-Steinberg dithering to simulate grayscale
    This makes images look much better on thermal printers
    """
    height, width = img_array.shape
    output = img_array.copy().astype(float)
    
    for y in range(height):
        for x in range(width):
            old_pixel = output[y, x]
            new_pixel = 255 if old_pixel > 128 else 0
            output[y, x] = new_pixel
            error = old_pixel - new_pixel
            
            # Distribute error to neighboring pixels
            if x + 1 < width:
                output[y, x + 1] += error * 7 / 16
            if y + 1 < height:
                if x > 0:
                    output[y + 1, x - 1] += error * 3 / 16
                output[y + 1, x] += error * 5 / 16
                if x + 1 < width:
                    output[y + 1, x + 1] += error * 1 / 16
    
    return output.astype(np.uint8)


def convert_to_bitmap(image, max_width=576, invert=False, align="center", mode="gsv0", dither=True):
    """
    Convert PIL Image to ESC/POS bitmap bytes
    
    Args:
        image: PIL Image object
        max_width: Maximum width in pixels (default 576 for 80mm printer)
        invert: Invert colors (black<->white)
        align: Alignment (left, center, right)
        mode: Print mode (gsv0 or gsv1)
        dither: Apply dithering for better grayscale simulation (default True)
    
    Returns:
        bytes: ESC/POS commands to print the image
    """
    # Convert to grayscale
    img = image.convert('L')
    
    # Resize if needed, maintaining aspect ratio
    if img.width > max_width:
        ratio = max_width / img.width
        new_height = int(img.height * ratio)
        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
    
    # Apply dithering for better quality
    if dither:
        img_array = np.array(img)
        img_array = floyd_steinberg_dithering(img_array)
        img = Image.fromarray(img_array, mode='L')
    
    # Convert to black and white
    img = img.point(lambda x: 0 if x < 128 else 255, '1')
    
    # Invert if requested
    if invert:
        img = img.point(lambda x: 255 - x)
    
    # Get image dimensions
    width = img.width
    height = img.height
    
    # Calculate bytes per line (each pixel is 1 bit, so divide by 8)
    bytes_per_line = (width + 7) // 8
    
    # Create output buffer
    output = bytearray()
    
    # Add alignment command
    align_cmd = {
        'left': b'\x1b\x61\x00',
        'center': b'\x1b\x61\x01',
        'right': b'\x1b\x61\x02'
    }.get(align.lower(), b'\x1b\x61\x01')
    output.extend(align_cmd)
    
    # Convert image to bitmap data
    # Using GS v 0 command (print raster bitmap)
    # Format: GS v 0 m xL xH yL yH d1...dk
    
    if mode == "gsv0":
        # GS v 0 - Print raster bitmap
        output.extend(b'\x1d\x76\x30')  # GS v 0
        output.append(0)  # m = 0 (normal mode)
        
        # Width in bytes (little endian)
        output.append(bytes_per_line & 0xFF)
        output.append((bytes_per_line >> 8) & 0xFF)
        
        # Height in pixels (little endian)
        output.append(height & 0xFF)
        output.append((height >> 8) & 0xFF)
        
        # Image data
        for y in range(height):
            line_data = bytearray()
            for x in range(0, width, 8):
                byte_val = 0
                for bit in range(8):
                    if x + bit < width:
                        pixel = img.getpixel((x + bit, y))
                        # Black pixel = 1, White pixel = 0
                        if pixel == 0:
                            byte_val |= (0x80 >> bit)
                line_data.append(byte_val)
            
            # Pad to bytes_per_line if needed
            while len(line_data) < bytes_per_line:
                line_data.append(0)
            
            output.extend(line_data)
    
    else:
        # Alternative mode: ESC * for bit image
        output.extend(b'\x1b\x2a')  # ESC *
        output.append(33)  # 24-dot double-density
        output.append(width & 0xFF)
        output.append((width >> 8) & 0xFF)
        
        for y in range(height):
            for x in range(width):
                pixel = img.getpixel((x, y))
                output.append(0xFF if pixel == 0 else 0x00)
    
    # Reset alignment to left
    output.extend(b'\x1b\x61\x00')
    
    return bytes(output)


def main():
    parser = argparse.ArgumentParser(description='Convert image to ESC/POS format')
    parser.add_argument('image', help='Input image file')
    parser.add_argument('--max-width', type=int, default=576, 
                        help='Maximum width in pixels (default: 576)')
    parser.add_argument('--mode', choices=['gsv0', 'gsv1'], default='gsv0',
                        help='Print mode (default: gsv0)')
    parser.add_argument('--align', choices=['left', 'center', 'right'], 
                        default='center', help='Image alignment (default: center)')
    parser.add_argument('--invert', action='store_true', 
                        help='Invert colors (black<->white)')
    parser.add_argument('--no-dither', action='store_true',
                        help='Disable dithering (results in pure black/white)')
    
    args = parser.parse_args()
    
    try:
        # Load image
        img = Image.open(args.image)
        
        # Convert to ESC/POS format
        bitmap_data = convert_to_bitmap(
            img,
            max_width=args.max_width,
            invert=args.invert,
            align=args.align,
            mode=args.mode,
            dither=not args.no_dither
        )
        
        # Write to stdout (binary mode)
        sys.stdout.buffer.write(bitmap_data)
        
    except FileNotFoundError:
        print(f"Error: Image file '{args.image}' not found", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error processing image: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

