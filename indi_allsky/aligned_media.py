"""Render a common geographic sky from a frame's own stellar calibration."""
import math

import cv2
import numpy as np

from .fisheye import SIN45, projectToPixels


def projection_values(solution):
    v = solution['values']
    return (v['AZIMUTH_ANGLE'], 0, 0, v['IMAGE_CIRCLE_DIAMETER'], v['OFFSET_X'],
            v['OFFSET_Y'], v['RADIAL_DISTORTION'], v['LENS_ALTITUDE'], v['POINTING_AZIMUTH'])


def frame_geometry(metadata, size):
    """Only use well-formed geometry belonging to the archived image dimensions."""
    if not isinstance(metadata, dict):
        return None
    solution = metadata.get('star_alignment')
    if not isinstance(solution, dict) or solution.get('image_size') != list(size):
        return None
    try:
        params = projection_values(solution)
        if (not all(type(n) in (float, int) and math.isfinite(n) for n in params)
                or params[3] <= 0 or not -.5 <= params[6] <= 1 or not 0 <= params[7] <= 90):
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return solution


def horizon_edge(solution, azimuth, width, height):
    """Where a bearing meets the rectangular image border, seen from the zenith."""
    params = projection_values(solution)
    x, y = projectToPixels(np.array([math.pi / 2, 0.]), azimuth, params[:7], width, height,
                           lens_altitude=params[7], pointing_azimuth=params[8])
    dx, dy = x[1] - x[0], y[1] - y[0]
    # Clamp the origin inside the drawable image for very steep camera tilts.
    x0, y0 = np.clip(x[0], 0, width-1), np.clip(y[0], 0, height-1)
    edges = []
    if abs(dx) > 1e-8:
        edges.append(((width-1 if dx > 0 else 0) - x0) / dx)
    if abs(dy) > 1e-8:
        edges.append(((height-1 if dy > 0 else 0) - y0) / dy)
    t = min(edges, default=0)
    return int(round(x0 + t * dx)), int(round(y0 + t * dy))


class SkyRemap:
    """Cache one OpenCV lookup map. North is up, east left, zenith centered."""
    def __init__(self):
        self.key = None
        self.maps = None

    def image(self, image, solution, *, border=(0, 0, 0, 0)):
        h, w = image.shape[:2]
        final_w, final_h = solution['image_size']
        left, top, right, bottom = border
        params = projection_values(solution)
        args = (w, h, final_w, final_h, left, top, right, bottom, params)
        if args != self.key:
            self.maps = self._maps(*args)
            self.key = args
        return cv2.remap(image, *self.maps, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

    @staticmethod
    def _maps(w, h, final_w, final_h, left, top, right, bottom, params):
        # Invert the output's centered equisolid model, then project that sky
        # direction into the actual lens. Work in strips on memory-limited Pis.
        map1 = np.empty((h, w, 2), np.int16)
        map2 = np.empty((h, w), np.uint16)
        x = (np.arange(w, dtype=np.float32) - w / 2) / (min(w, h) / 2)
        scale_x = w / (final_w - left - right)
        scale_y = h / (final_h - top - bottom)
        for start in range(0, h, 64):
            end = min(start + 64, h)
            y = (np.arange(start, end, dtype=np.float32)[:, None] - h / 2) / (min(w, h) / 2)
            radius = np.hypot(x, y)
            alt = np.pi / 2 - 2 * np.arcsin(np.minimum(1., radius) * SIN45)
            az = np.arctan2(-x, -y)
            sx, sy = projectToPixels(alt, az, params[:7], final_w, final_h,
                                     lens_altitude=params[7], pointing_azimuth=params[8])
            # cv2.resize samples at pixel centers. Undo scale and border when
            # working on the intermediate capture image before label rendering.
            sx = ((sx - left + .5) * scale_x - .5).astype(np.float32)
            sy = ((sy - top + .5) * scale_y - .5).astype(np.float32)
            sx[radius > 1.] = -2
            sy[radius > 1.] = -2
            map1[start:end], map2[start:end] = cv2.convertMaps(sx, sy, cv2.CV_16SC2)
        return map1, map2
