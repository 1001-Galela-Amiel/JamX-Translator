import numpy as np
from PIL import Image
import Quartz

def capture_screen():
    """Capture full screen using Quartz."""
    main_display_id = Quartz.CGMainDisplayID()
    image_ref = Quartz.CGDisplayCreateImage(main_display_id)
    width = Quartz.CGImageGetWidth(image_ref)
    height = Quartz.CGImageGetHeight(image_ref)
    bpr = Quartz.CGImageGetBytesPerRow(image_ref)
    data_provider = Quartz.CGImageGetDataProvider(image_ref)
    data = Quartz.CGDataProviderCopyData(data_provider)

    arr = np.frombuffer(data, dtype=np.uint8)
    arr = arr[:height * bpr]
    arr = arr.reshape((height, bpr))
    arr = arr[:, :width*4]
    arr = arr.reshape((height, width, 4))
    # BGRA -> RGB
    rgb = arr[:, :, [2, 1, 0]]
    return rgb

def capture_window_image(hwnd: int) -> Image.Image:
    """Capture a specific window using Quartz."""
    from Quartz import CGWindowListCreateImage, kCGWindowImageBoundsIgnoreFraming, kCGWindowListOptionIncludingWindow, CGRectNull, CGImageGetWidth, CGImageGetHeight, CGImageGetDataProvider, CGDataProviderCopyData

    image_ref = CGWindowListCreateImage(
        CGRectNull,
        kCGWindowListOptionIncludingWindow,
        hwnd,
        kCGWindowImageBoundsIgnoreFraming
    )
    if image_ref is None:
        return None

    width = CGImageGetWidth(image_ref)
    height = CGImageGetHeight(image_ref)
    bpr = Quartz.CGImageGetBytesPerRow(image_ref)
    data = CGDataProviderCopyData(CGImageGetDataProvider(image_ref))

    arr = np.frombuffer(data, dtype=np.uint8)
    arr = arr[:height * bpr]
    arr = arr.reshape((height, bpr))
    arr = arr[:, :width*4]
    arr = arr.reshape((height, width, 4))
    arr = arr[:, :, [2, 1, 0, 3]] 

    return Image.fromarray(arr, mode="RGBA")