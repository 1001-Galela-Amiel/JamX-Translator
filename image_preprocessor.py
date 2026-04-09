import os
import time
import numpy as np
import cv2
from PIL import Image, ImageEnhance, ImageFilter

"""Function to aid in preprocessing of images inserted into RapidOCR to increased accuracy by allowing for removal of background behind text"""
"""Taken from Minh Shiba's (creator of Sugoi Toolkit) now deprecated "Visual-Novel-OCR" https://github.com/leminhyen2/Visual-Novel-OCR, GPL 3.0"""

"""H refers to hue (color values), S refers to saturation, V refers to brightness"""
"""Requires image in VC2 format (np.array on PIL Images)"""
def removeBackground(img, hMin, sMin, vMin, hMax, sMax, vMax, binarizedValue):

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
