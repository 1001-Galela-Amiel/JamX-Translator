import os
import time
import numpy as np
import cv2
from PIL import Image, ImageEnhance, ImageFilter
import json

"""Function to aid in preprocessing of images inserted into RapidOCR to increased accuracy by allowing for removal of background behind text"""
"""Taken from Minh Shiba's (creator of Sugoi Toolkit) now deprecated "Visual-Novel-OCR" https://github.com/leminhyen2/Visual-Novel-OCR, GPL 3.0"""

"""H refers to hue (color values), S refers to saturation, V refers to brightness"""
"""Requires image in CV2 format (np.array on PIL Images)"""
"""Gets variables from preprocessing_settings.json if not parameterized"""
def removeBackground(img, h_min=None, s_min=None, v_min=None, h_max=None, s_max=None, v_max=None, binarize=None):
    check_none = [h_min, s_min, v_min, h_max, s_max, v_max, binarize]
    
    # Check if parameterized
    if any(value is None for value in check_none):
        # If no parameters, load .json. If nothing in .json, load default
        if os.path.exists("preprocessing_settings.json"):
            with open("preprocessing_settings.json", "r") as f:
                data = json.load(f)
            try:
                hMin = data["h_min"]
                sMin = data["s_min"]
                vMin = data["v_min"]
                hMax = data["h_max"]
                sMax = data["s_max"]
                vMax = data["v_max"]
                binarizedValue = data["binarization"]
            except KeyError:
                hMin = 0
                sMin = 0
                vMin = 0
                hMax = 179
                sMax = 255
                vMax = 255

    # Otherwise, load parameters
    else:
        hMin = h_min
        sMin = s_min
        vMin = v_min
        hMax = h_max
        sMax = s_max
        vMax = v_max
        binarizedValue = binarize
        
    # Set minimum and max HSV values to display
    lower = np.array([hMin, sMin, vMin])
    upper = np.array([hMax, sMax, vMax])

    # Create HSV Image and threshold into a range.
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, lower, upper)
    output = cv2.bitwise_and(img,img, mask= mask)

    ret, inverseBinarizedOutput = cv2.threshold(output,binarizedValue,255,cv2.THRESH_BINARY_INV)

    cv2.imwrite("colorChangedImage.png", inverseBinarizedOutput)

    return inverseBinarizedOutput
