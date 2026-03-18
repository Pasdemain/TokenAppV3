import base64

# This creates a simple purple square as a placeholder icon
# You should replace this with a proper icon

def create_simple_icon():
    # Create a simple 1x1 purple pixel PNG
    # This is just a placeholder - you should use a real icon
    purple_pixel = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8cf\xf0\x0f\x00\x02\x81\x01\x86\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
    
    # Save as 192x192
    with open('static/icon-192.png', 'wb') as f:
        f.write(purple_pixel)
    
    # Save as 512x512 
    with open('static/icon-512.png', 'wb') as f:
        f.write(purple_pixel)
    
    print("Icons created (placeholders)")

if __name__ == "__main__":
    create_simple_icon()
