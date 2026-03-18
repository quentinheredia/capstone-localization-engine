from typing import List, Tuple, Optional
import math
import numpy as np
from scipy.optimize import least_squares

def rssi_to_distance_m(rssi_dbm: float, p0_dbm: float, n: float) -> float:
    d = 10.0 ** ((p0_dbm - rssi_dbm) / (10.0 * n))
    return max(min(d, 50.0), 0.05)

def bounded_trilaterate(anchors, dists, room_w, room_h, initial_guess: Optional[Tuple[float, float]] = None):
    def residuals(p):
        x, y = p
        return [
            math.hypot(x - ax, y - ay) - d
            for (ax, ay), d in zip(anchors, dists)
        ]

    if initial_guess is not None:
        x0, y0 = initial_guess
    else:
        weights = [1.0 / (d**2) for d in dists]
        total_weight = sum(weights)
        if total_weight > 0:
            x0 = sum(a[0] * w for a, w in zip(anchors, weights)) / total_weight
            y0 = sum(a[1] * w for a, w in zip(anchors, weights)) / total_weight
        else:
            x0 = np.mean([a[0] for a in anchors])
            y0 = np.mean([a[1] for a in anchors])

    sol = least_squares(
        residuals,
        x0=[x0, y0],
        bounds=([0.0, 0.0], [room_w, room_h]),
        max_nfev=200
    )
    return float(sol.x[0]), float(sol.x[1])

def refined_trilaterate(anchors, dists, room_w, room_h, initial_guess: Optional[Tuple[float, float]] = None):
    goodA, goodD = [], []
    for (ax, ay), d in zip(anchors, dists):
        if 0.05 <= d <= 50:
            goodA.append((ax, ay))
            goodD.append(d)

    # Allow 2 APs for Zone Isolation logic (2 circles intersect/approach)
    if len(goodA) < 2:
        return room_w/2, room_h/2

    return bounded_trilaterate(goodA, goodD, room_w, room_h, initial_guess)