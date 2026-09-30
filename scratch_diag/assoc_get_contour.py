"""
Get a real, algorithmically-derived "confirmed polygon" (simplified vertex
list) for 27211ae5 p13's shared-edge child contour (Lot 48-3), already
verified correct earlier this session via morphological closing +
RETR_CCOMP. Used as ground truth for the label-to-edge association spike --
not invented, this is the same contour already validated visually.
"""
import cv2
import numpy as np
import json

PATH = "data/documents/27211ae5-0099-4b0d-8176-f1483897b766/pages/page_013.png"
img = cv2.imread(PATH)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
_, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
contours, hierarchy = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)

# find the two children of the big merged parent (as found earlier this session)
big = [(i, c) for i, c in enumerate(contours) if cv2.contourArea(c) > 3000]
big.sort(key=lambda ic: -cv2.contourArea(ic[1]))
parent_idx = big[0][0]
children = [(i, c) for i, c in big if hierarchy[0][i][3] == parent_idx]
print("children found:", len(children), [(i, cv2.contourArea(c)) for i, c in children])

for i, c in children:
    peri = cv2.arcLength(c, True)
    simplified = cv2.approxPolyDP(c, 0.005 * peri, True)
    pts = [[int(p[0][0]), int(p[0][1])] for p in simplified]
    print(f"contour {i}: {len(pts)} vertices after simplification: {pts}")
