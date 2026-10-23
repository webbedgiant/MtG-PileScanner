"""Find card-shaped rectangles (whole cards OR just the artwork window) and rectify them upright."""
import cv2, numpy as np

CW, CH = 252, 352   # rectified card
AW, AH = 168, 116   # rectified art window


def find_rects(img, max_side=1600, min_area=0.0004, max_area=0.3, aspect=(0.60, 0.82)):
    """Rectangles with a card-like aspect ratio (0.60-0.82). A card (0.716) and its art window (~0.70) both qualify."""
    H, W = img.shape[:2]
    s = min(1.0, max_side / max(H, W))
    im = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img
    h, w = im.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    found = []
    for lo, hi in ((25, 80), (50, 140)):
        edges = cv2.Canny(gray, lo, hi)
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            area = cv2.contourArea(c)
            if area < min_area * h * w or area > max_area * h * w:
                continue
            rect = cv2.minAreaRect(c)
            (cx, cy), (rw, rh), _ = rect
            if min(rw, rh) < 30 or max(rw, rh) == 0:
                continue
            if not aspect[0] <= min(rw, rh) / max(rw, rh) <= aspect[1]:
                continue
            if cv2.contourArea(cv2.convexHull(c)) / (rw * rh) < 0.80:
                continue
            found.append((rw * rh / (s * s), (cx / s, cy / s), min(rw, rh) / s, cv2.boxPoints(rect) / s))
    found.sort(key=lambda t: -t[0])
    kept = []
    for area, ctr, side, pts in found:  # drop duplicates (inner/outer edge, both Canny passes)
        if any(np.hypot(ctr[0] - k[1][0], ctr[1] - k[1][1]) < 0.25 * min(side, k[2])
               and 0.8 < area / k[0] < 1.25 for k in kept):
            continue
        kept.append((area, ctr, side, pts))
    return [k[3].astype(np.float32) for k in kept]


def find_whole_cards(img, max_side=1000):
    """Fast detector for cards lying separately: only full-size card outlines (no art windows), largest first."""
    rects = find_rects(img, max_side=max_side, min_area=0.012, max_area=0.92, aspect=(0.64, 0.80))
    kept = []
    for p in rects:  # already sorted by area, biggest first
        a, c = cv2.contourArea(p), (float(p[:, 0].mean()), float(p[:, 1].mean()))
        if any(a < 0.65 * ka and cv2.pointPolygonTest(kp.reshape(-1, 1, 2), c, False) >= 0 for kp, ka in kept):
            continue  # a smaller box inside a bigger card = its art window or text box, not another card
        kept.append((p, a))
    return [p for p, _ in kept]


def explained(pts, centers):
    """True if an OCR-identified title sits inside this rectangle (card hypothesis) or just above/below it
    (art-window hypothesis), meaning the text pass already handled that card."""
    c = pts.mean(axis=0)
    e1, e2 = pts[1] - pts[0], pts[2] - pts[1]
    if np.linalg.norm(e1) < np.linalg.norm(e2):
        e1, e2 = e2, e1
    L, S = np.linalg.norm(e1), np.linalg.norm(e2)
    a, b = e1 / L, e2 / S
    for p in centers:
        d = np.array(p) - c
        xl, xs = abs(d @ a), abs(d @ b)
        if xl <= L / 2 and xs <= S / 2:
            return True
        if xl <= 0.6 * L and S / 2 <= xs <= S / 2 + 0.5 * S:
            return True
    return False


def _order(pts):
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def _warp(img, pts, w, h, long_vertical):
    p = _order(pts)
    wd = (np.linalg.norm(p[1] - p[0]) + np.linalg.norm(p[2] - p[3])) / 2
    ht = (np.linalg.norm(p[3] - p[0]) + np.linalg.norm(p[2] - p[1])) / 2
    if (wd > ht) == long_vertical:  # long side is on the wrong axis: restart from the next corner
        p = np.roll(p, -1, axis=0)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(p, dst), (w, h))


def warp_card(img, pts, scale=1):  # rectangle is a whole card (long side vertical)
    return _warp(img, pts, CW * scale, CH * scale, True)


def warp_art(img, pts):   # rectangle is the art window (long side horizontal)
    return _warp(img, pts, AW, AH, False)


warp = warp_card
