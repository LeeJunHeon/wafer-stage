"""detect.py - 웨이퍼 원판/샘플 검출 엔진.

GUI 의존성이 전혀 없다. BGR 이미지 한 장과 파라미터 dict 를 넣으면
DetectResult 를 돌려주는 순수 모듈이라, 저장된 사진으로 따로 돌려볼 수 있다.

좌표계: 원점 = 웨이퍼 중심, +X 오른쪽, +Y 위쪽, 단위 mm.
"""

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

# 무채색 원판 판정 기준. 배경(목재 책상, 투명 케이스)은 유채색이라는 성질을 쓴다.
# 실측: 웨이퍼 S 중앙값 18, 투명 케이스/나무 책상 S 중앙값 53~62.
# 45 는 경계에 너무 가까워 케이스 일부가 원판에 들러붙었다. 35 로 내린다.
SAT_MAX = 35
VAL_MIN = 60
# V 상한. 기본은 끔(256).
# 날아간 하이라이트를 빼면 케이스에 맺힌 반사광이 안 붙어서 좋을 것 같지만,
# 조명이 세면 '웨이퍼 자체' 가 먼저 포화된다. 실제로 250 을 쓰자 웨이퍼의
# 밝은 절반이 통째로 잘려나가 ROI 가 반쪽이 되고 샘플을 절반 놓쳤다.
# 웨이퍼는 안 날아가면서 배경만 날아가는 조명일 때만 켤 것.
VAL_MAX = 256
BLOWN_LEVEL = 250     # 이 이상은 센서가 포화된 것으로 본다 (진단 전용)

DEFAULTS = {
    "camera_index": 1,
    "backend": "auto",
    "width": 1280,
    "height": 720,
    "fourcc": "MJPG",
    "autofocus": True,    # 기본 켜기. 끄면 focus 값으로 직접 맞춰야 한다
    "auto_exposure": True,  # 끄려면 exposure 에 실제 값을 반드시 함께 지정할 것
    "af_settle_s": 5.0,   # 촬영 전 AF 수렴을 기다리는 최대 시간
    "af_min_wait_s": 0.5,  # 최소 이만큼은 기다린다 (AF 가 반응을 시작할 시간)
    "focus": -1,          # autofocus=false 일 때만 쓰임. 0~255 = 고정, "auto" = 스윕
    "focus_step": 15,     # 초점 스윕 간격 (굵게 훑은 뒤 1/4 간격으로 다시 훑는다)
    "focus_settle_s": 0.35,
    "exposure": 0,
    "capture_frames": 15,
    "wafer_diameter_mm": 100.0,
    "seed_pct": 15,
    "grow_pct": 80,
    "open_pct": 0.6,      # 잡티 제거 커널 = 이 % x 웨이퍼 반지름
    "close_pct": 0.6,     # 조각 잇기 커널. 키우면 이웃한 샘플끼리 붙어버린다
    "hull_fit": True,     # 실루엣 볼록껍질 피팅을 후보에 넣는다 (IoU 높은 쪽 채택)
    "merge_mm": 6.0,      # 이보다 가까운 덩어리는 한 샘플의 조각인지 검사한다
    "merge_solidity": 0.5,  # 합친 결과가 이만큼 볼록해야 실제로 합친다
    "sat_delta": 15,      # 18 -> 15: 비스듬한 촬영에서 샘플 하나를 더 건짐 (회귀 없음)
    "min_area_mm2": 3,
    "max_area_mm2": 150,
    # 가장자리 마진. 6/3 은 웨이퍼 반지름의 9% 를 죽여서, 가장자리 가까이 놓인
    # 샘플이 통째로 버려졌다 (실촬영에서 14개 중 3개 손실). 실루엣 교집합이
    # 이미 테두리를 걷어내므로 이만큼 크게 잡을 필요가 없다.
    "edge_margin_pct": 3,
    "edge_band_pct": 1.5,  # 이 폭의 테두리 띠. 여기 '잠긴' 비율로 링 잡티를 가린다
    # 테두리 띠에 '닿기만 해도 폐기' 하면 띠에 모서리를 스친 멀쩡한 샘플까지
    # 사라진다 (실촬영 14개 중 3개). 링 잡티는 띠를 따라 길게 누워 대부분이
    # 띠 안이고 샘플은 덩어리라 몇 % 뿐이므로, '띠 안 픽셀 비율' 로 가른다.
    "edge_touch_max": 0.15,
    "split_depth_mm": 1.0,   # 맞닿은 두 샘플을 가를 때 요구하는 홈 깊이
    "split_solidity": 0.85,  # 가른 조각/테두리 샘플에 요구하는 볼록도 (파편 0.74)
    "shape_fill": 0.8,       # 삼각형/사각형 채움비 판정 문턱
    # 색거리(dE) 보조 경로. 밝기도 채도도 웨이퍼와 비슷한데 '색조' 만 다른 조각을
    # 잡는다 (실측 113853: 밝기 편차 6~10%(임계 15), 채도차 +1~+3(임계 15) 인데
    # Lab 색거리는 10~35, 웨이퍼 배경은 2~3).
    "de_floor": 8.0,         # 색거리 임계의 절대 하한
    "de_k": 4.0,             # 배경 색잡음(ROI 안 dE 중앙값) 대비 배수. 0 = 끔
    # 엣지 보완. 밝은 회색 칩은 색 마스크로 조각의 일부(실측 60%, 39%)만 잡혀
    # 중심이 0.9~2.1mm 밀렸다. 같은 칩이 Canny 엣지에서는 닫힌 다각형으로
    # 온전히 나오므로, 이미 확정된 샘플에 한해 그 다각형으로 윤곽을 갈아끼운다.
    # 새 샘플은 절대 만들지 않는다 (마스크 파이프라인의 판정은 그대로 존중).
    "edge_complete": True,
    "edge_canny": [[20, 60], [12, 40]],   # 위에서 실패하면 아래로 (사다리)
    "edge_cover_min": 0.6,       # 후보가 조각 픽셀의 이만큼을 덮어야 한다
    "edge_grow_max": 3.0,        # 조각 면적의 이 배를 넘으면 다른 것까지 삼킨 것
    "edge_solidity_min": 0.85,   # 후보 다각형의 볼록도
    "edge_vertices_max": 6,      # 꼭짓점 3~6개 (칩은 삼각/사각/오각)
    "edge_other_overlap_max": 0.3,  # 다른 샘플을 이만큼 넘게 물면 버린다
    "sat_max": 35,        # 원판 판정 채도 상한. 배경이 원판에 붙으면 낮춘다
    "val_min": 60,
    "val_max": 256,       # 원판 판정 밝기 상한. 256 = 끔 (11절 설명 참고)
    "min_focus_score": 0,   # 선명도 절대 하한 (0 = 끔). 수동초점 스윕이 값을 채운다
    # 표면 모드. 웨이퍼가 없으면 기준 두 개(스케일 = 100mm/장축px, 원점 = 원판
    # 중심)가 같이 사라진다. 철판 모드는 그 둘을 아래 보정값과 화면 중심으로
    # 대체한다. "auto" 는 원판을 먼저 찾아 보고, 보정값이 있으면 크기 앵커로
    # 진짜 원판인지 검사한다 (실측: 웨이퍼가 아예 없는 철판 사진에서도 엣지
    # 경로가 잡티로 지지율 0.53 짜리 가짜 타원을 만들었다).
    # 철판에 붙인 ArUco 마커(30mm)로 촬영마다 스케일을 그 자리에서 잰다.
    # 카메라 높이가 바뀌어도, 웨이퍼가 없어도 스케일이 정확하다 - 높이에 묶인
    # plate_mm_per_px 보정이 깨지던 문제의 근본 해법.
    "marker_mm": 30.0,        # 마커 한 변의 실제 길이. 0 = 마커 기능 끔
    "marker_dict": "4X4_50",
    "surface": "auto",        # "wafer" | "plate" | "auto"
    "plate_mm_per_px": 0,     # 철판 모드 스케일. 0 = 미보정 (좌표가 픽셀 단위)
    "plate_margin_pct": 2,    # 철판 모드 ROI 여백 (min(H,W) 대비 %)
    # 경로: data_dir 은 코드 폴더 기준 상대경로, out_dir 은 data_dir 기준.
    # 실제 경로 계산은 paths.py 가 한다 (detect.py 는 파일을 직접 쓰지 않는다).
    "data_dir": "../data",
    "out_dir": "out",
}


# --------------------------------------------------------------------------
# 결과 컨테이너
# --------------------------------------------------------------------------
@dataclass
class Wafer:
    found: bool = False
    center_px: tuple = (0.0, 0.0)
    major_px: float = 0.0
    minor_px: float = 0.0
    axis_ratio: float = 0.0
    theta_deg: float = 0.0
    tilt_deg: float = 0.0
    mm_per_px: float = 0.0
    clipped_by_frame: bool = False
    flat_angle_deg: object = None
    flat_line_px: object = None        # [[x,y],[x,y]] 전체 해상도. 없으면 None
    straight_edges_px: list = field(default_factory=list)  # 0.45r 이상 직선 변 전부
    roi_fit: float = 0.0
    ray_coverage: float = 0.0          # 쓸 수 있었던 광선 비율 (1.0 = 360방향 전부)
    ellipse_over_silhouette: float = 0.0  # 피팅 타원 면적 / 실루엣 면적
    blown_pct: float = 0.0             # 원판 안에서 하이라이트가 날아간 비율 %
    silhouette_solidity: float = 0.0   # 실루엣 볼록도. 원판은 플랫이 있어도 볼록하다
    surface: str = "wafer"             # "wafer" = 원판 위, "plate" = 철판 위
    scale_source: str = "wafer"        # "wafer" | "marker" | "settings" | "none"
    sat_max_used: int = 0              # 색 경로가 실제로 채택한 채도 상한 (0 = 엣지 경로)
    outline_source: str = "color"      # "color" = 무채색 마스크, "edge" = 엣지 피팅
    outline_support: float = 0.0       # 고른 타원의 둘레 지지율 (Canny 엣지에 얹힌 비율)

    def as_dict(self):
        return {
            "found": self.found,
            "center_px": [round(self.center_px[0], 1), round(self.center_px[1], 1)],
            "major_px": round(self.major_px, 1),
            "minor_px": round(self.minor_px, 1),
            "axis_ratio": round(self.axis_ratio, 4),
            "theta_deg": round(self.theta_deg, 1),
            "tilt_deg": round(self.tilt_deg, 1),
            "mm_per_px": round(self.mm_per_px, 5),
            "clipped_by_frame": self.clipped_by_frame,
            "flat_angle_deg": (None if self.flat_angle_deg is None
                               else round(float(self.flat_angle_deg), 1)),
            "flat_line_px": (None if self.flat_line_px is None else
                             [[round(float(x), 1), round(float(y), 1)]
                              for x, y in self.flat_line_px]),
            "straight_edges": len(self.straight_edges_px),
            "roi_fit": round(self.roi_fit, 3),
            "ray_coverage": round(self.ray_coverage, 3),
            "ellipse_over_silhouette": round(self.ellipse_over_silhouette, 3),
            "blown_pct": round(self.blown_pct, 1),
            "silhouette_solidity": round(self.silhouette_solidity, 3),
            "surface": self.surface,
            "scale_source": self.scale_source,
            "sat_max_used": int(self.sat_max_used),
            "outline_source": self.outline_source,
            "outline_support": round(self.outline_support, 3),
        }


@dataclass
class DetectResult:
    wafer: Wafer = field(default_factory=Wafer)
    samples: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    info: list = field(default_factory=list)   # 경고가 아닌 안내 (보정 힌트 등)
    markers: list = field(default_factory=list)       # [{"id","center_px","side_px"}]
    marker_polys: list = field(default_factory=list)  # 그리기/ROI 제외용 네 꼭짓점
    detect_ms: float = 0.0
    # 디버그 이미지들
    mask: object = None
    dev_img: object = None
    wafer_mask: object = None
    roi: object = None


# --------------------------------------------------------------------------
# 작은 유틸
# --------------------------------------------------------------------------
def _disk(r):
    r = max(1, int(round(r)))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _largest_component(mask):
    n, lab, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    if n <= 1:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (lab == idx).astype(np.uint8) * 255


def _fill_holes(mask):
    """바깥에서 flood fill 한 뒤 반전 = 내부 구멍. 샘플이 뚫은 구멍을 메운다."""
    h, w = mask.shape
    ff = mask.copy()
    # 테두리가 이미 채워져 있을 수 있으므로 1px 여백을 두고 채운다.
    pad = cv2.copyMakeBorder(ff, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    tmp = np.zeros((h + 4, w + 4), np.uint8)
    cv2.floodFill(pad, tmp, (0, 0), 255)
    ff = pad[1:-1, 1:-1]
    return mask | cv2.bitwise_not(ff)


def _touches_border(mask):
    return bool(mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any())


# --------------------------------------------------------------------------
# 7-1. 웨이퍼 원판 찾기
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 7-0. ArUco 마커 스케일
# --------------------------------------------------------------------------
_ARUCO_WARNED = [False]


def find_markers(bgr, params):
    """철판에 붙인 ArUco 마커에서 mm/px 를 잰다.

    한 변이 marker_mm 인 정사각형이므로 사진 속 변 길이만 재면 스케일이 나온다.
    웨이퍼 장축과 달리 원판이 없어도, 카메라 높이가 바뀌어도 그 자리에서 맞는
    값이 나온다. 여러 개면 중앙값 (한 장이 구겨져도 흔들리지 않게).

    돌려주는 값: (mm_per_px 또는 None, [{"id","center_px","side_px"}...],
                 [네 꼭짓점 배열...], 경고 목록)
    """
    mm_target = float(params.get("marker_mm", 0) or 0)
    if mm_target <= 0:
        return None, [], [], []
    aruco = getattr(cv2, "aruco", None)
    if aruco is None or not hasattr(aruco, "ArucoDetector"):
        # OpenCV 4.7 미만. 기능만 끄고 검출은 계속한다.
        if _ARUCO_WARNED[0]:
            return None, [], [], []
        _ARUCO_WARNED[0] = True
        return None, [], [], ["cv2.aruco unavailable; upgrade opencv-python"]
    name = "DICT_" + str(params.get("marker_dict", "4X4_50"))
    dic = aruco.getPredefinedDictionary(getattr(aruco, name, aruco.DICT_4X4_50))
    corners, ids, _rej = aruco.ArucoDetector(dic, aruco.DetectorParameters()).detectMarkers(bgr)
    if ids is None or len(ids) == 0:
        return None, [], [], []
    # 케이스 반사에서 가짜 마커가 읽히는 일이 있다 (실측 115011: 한 변 26.4px
    # 짜리 id17 -> mm/px 1.136, 즉 100mm 원판이 88px 이어야 한다는 뜻이라 물리적으로
    # 말이 안 된다). 웨이퍼를 못 찾은 사진에서는 이 가짜가 그대로 스케일이 되므로
    # 여기서 걸러야 한다. 기준은 원판 찾기의 MIN_FILL 과 같다.
    H, W = bgr.shape[:2]
    base = min(H, W)
    wd = float(params.get("wafer_diameter_mm", 100.0))
    raw, dropped = [], 0
    for c, i in zip(corners, ids.reshape(-1)):
        q = np.asarray(c).reshape(4, 2).astype(np.float32)
        side = float(np.mean([np.linalg.norm(q[k] - q[(k + 1) % 4]) for k in range(4)]))
        if side <= 1:
            dropped += 1
            continue
        disc_px = side * wd / mm_target        # 이 마커가 함의하는 원판 지름
        if not (MIN_FILL * base <= disc_px <= 3.0 * base):
            dropped += 1
            continue
        raw.append((int(i), q, side))
    if not raw:
        return None, [], [], (["ignored %d implausible marker(s)" % dropped]
                              if dropped else [])
    # 여러 개면 서로 검산한다. 중앙값에서 10% 넘게 벗어난 것은 구겨졌거나 가짜다.
    med = float(np.median([mm_target / r[2] for r in raw]))
    keep = [r for r in raw if abs(mm_target / r[2] - med) <= 0.10 * med]
    dropped += len(raw) - len(keep)
    if not keep:
        return None, [], [], ["ignored %d implausible marker(s)" % dropped]
    out, polys, scales = [], [], []
    for i, q, side in keep:
        out.append({"id": int(i),
                    "center_px": [round(float(q[:, 0].mean()), 1),
                                  round(float(q[:, 1].mean()), 1)],
                    "side_px": round(side, 1)})
        polys.append(q)
        scales.append(mm_target / side)
    warns = ["ignored %d implausible marker(s)" % dropped] if dropped else []
    return float(np.median(scales)), out, polys, warns


def find_wafer(bgr, params):
    """원판을 찾아 (Wafer, 전체해상도 실루엣 마스크) 를 돌려준다.

    원판 탐색은 가로 480px 축소본으로 충분하다 (형태 연산이 훨씬 안정적이고,
    장축 길이 정밀도는 이미 충분하다).
    """
    H, W = bgr.shape[:2]
    sc = 480.0 / W
    small = cv2.resize(bgr, (480, max(1, int(round(H * sc)))), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    S, V = hsv[:, :, 1], hsv[:, :, 2]
    smax = int(params.get("sat_max", SAT_MAX))
    vmin = int(params.get("val_min", VAL_MIN))
    vmax = int(params.get("val_max", VAL_MAX))
    m = ((S < smax) & (V > vmin) & (V < vmax)).astype(np.uint8) * 255

    # 함정 1: OPEN 이 반드시 먼저. CLOSE 를 먼저 하면 케이스 테두리의 저채도
    # 잡티가 원판에 들러붙어 원이 15% 부풀어 오른다.
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, _disk(6))
    # 함정 2: 구멍 메우기는 최대 연결요소를 고른 "뒤"에.
    m = _largest_component(m)
    if m is None:
        return Wafer(), np.zeros((H, W), np.uint8)
    m = _fill_holes(m)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, _disk(4))

    clipped = _touches_border(m)
    full = cv2.resize(m, (W, H), interpolation=cv2.INTER_NEAREST)

    sil_sol = 0.0
    _cs1, _h1 = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if _cs1:
        _c1 = max(_cs1, key=cv2.contourArea)
        _ha1 = cv2.contourArea(cv2.convexHull(_c1))
        sil_sol = float(cv2.contourArea(_c1) / _ha1) if _ha1 > 0 else 0.0

    # 거리변환 최댓값 = 원판 안쪽이 보장된 시드점
    dist = cv2.distanceTransform((m > 0).astype(np.uint8), cv2.DIST_L2, 5)
    sy, sx = np.unravel_index(int(np.argmax(dist)), dist.shape)
    h2, w2 = m.shape

    # 360방향 광선 스캔
    pts, dists, angs = [], [], []
    maxlen = int(math.hypot(w2, h2))
    for i in range(360):
        a = math.radians(i)
        ca, sa = math.cos(a), math.sin(a)
        last = None
        out_of_frame = False
        for t in range(1, maxlen):
            x = int(round(sx + ca * t))
            y = int(round(sy + sa * t))
            if x < 0 or y < 0 or x >= w2 or y >= h2:
                out_of_frame = True
                break
            if m[y, x] == 0:
                last = (sx + ca * (t - 1), sy + sa * (t - 1))
                break
            last = (float(x), float(y))
        # 함정 7: 화면 밖에서 끝난 광선은 가짜 경계이므로 버리고 남은 호만 쓴다.
        if out_of_frame or last is None:
            continue
        pts.append(last)
        dists.append(math.hypot(last[0] - sx, last[1] - sy))
        angs.append(a)

    if len(pts) < 12:
        return Wafer(clipped_by_frame=clipped), full

    dists = np.asarray(dists, float)
    med = float(np.median(dists))
    keep = np.abs(dists - med) < 0.10 * med
    if keep.sum() < 8:
        keep = np.ones_like(dists, bool)
    fit_pts = np.asarray([pts[i] for i in range(len(pts)) if keep[i]], np.float32)
    if len(fit_pts) < 5:
        return Wafer(clipped_by_frame=clipped), full

    (ecx, ecy), (ew, eh), eang = cv2.fitEllipse(fit_pts)

    # fitEllipse 의 angle 은 w 축 방향. w >= h 면 그게 장축, 아니면 +90.
    def split(ew, eh, eang):
        return (ew, eh, eang) if ew >= eh else (eh, ew, eang + 90.0)

    major, minor, theta_deg = split(ew, eh, eang)
    if major <= 0 or minor <= 0:
        return Wafer(clipped_by_frame=clipped), full

    P = np.asarray(pts, float)

    def ell_ratio(cx_, cy_, major_, minor_, theta_):
        """각 경계점이 그 방향의 타원 반지름 대비 얼마나 안쪽인지."""
        dx, dy = P[:, 0] - cx_, P[:, 1] - cy_
        rad = np.hypot(dx, dy)
        t = np.arctan2(dy, dx) - math.radians(theta_)
        r_e = 1.0 / np.sqrt((np.cos(t) / (major_ / 2.0)) ** 2 +
                            (np.sin(t) / (minor_ / 2.0)) ** 2)
        return dx, dy, rad, rad / np.maximum(r_e, 1e-6)

    # 플랫 구간은 중앙값 필터(±10%)만으로는 안 걸러진다 (100mm 웨이퍼의 표준
    # 플랫은 반지름의 94.6% 라 통과한다). 그대로 두면 단축이 끌려 내려가
    # 기울기가 부풀고 플랫도 안 보인다. 그래서 타원 반지름 대비 비율의
    # 양쪽 꼬리를 잘라내며 두 번 재피팅한다. 위쪽 꼬리도 자르는 이유:
    # 원판 밖으로 튀어나온 점은 정당한 이유가 없다 (반사광/케이스 잡티뿐).
    #
    # 트리밍 폭을 ±3% 로 고정했더니, 경계 잡음이 그보다 크면 한쪽에 몰린 점만
    # 남아 호(arc) 피팅이 무너졌다 (실촬영에서 타원이 원판의 51% 크기로 붕괴).
    # 그래서 (1) 폭을 잔차 산포(MAD)에 맞춰 넓히고,
    #        (2) 매 단계의 후보를 '실루엣 면적과 얼마나 맞는가' 로 채점해
    #            가장 잘 맞는 것을 고른다. 원판은 볼록하므로 타원 면적은
    #            실루엣 면적과 같아야 한다 (플랫이 있어도 1.6% 차이).
    # 채점은 면적이 아니라 실루엣과의 겹침(IoU)으로 한다. 면적만 보면 가늘고
    # 긴 타원이 우연히 같은 면적으로 통과해 버린다.
    silb = m > 0

    def fit_score(cx_, cy_, major_, minor_, theta_):
        e = np.zeros_like(m)
        cv2.ellipse(e, (int(round(cx_)), int(round(cy_))),
                    (max(1, int(major_ / 2)), max(1, int(minor_ / 2))),
                    theta_, 0, 360, 255, -1)
        eb = e > 0
        uni = np.count_nonzero(eb | silb)
        return (np.count_nonzero(eb & silb) / uni) if uni else 0.0

    best = (fit_score(ecx, ecy, major, minor, theta_deg),
            ecx, ecy, major, minor, theta_deg)
    for _ in range(2):
        _dx, _dy, _rad, ratio = ell_ratio(ecx, ecy, major, minor, theta_deg)
        med_r = float(np.median(ratio))
        mad = float(np.median(np.abs(ratio - med_r)))
        halfw = max(0.03, 2.5 * mad)
        # 위쪽 한계는 중앙값이 아니라 85퍼센타일 기준이어야 한다. 플랫이 크면
        # 중앙값이 아래로 끌려가 둥근 쪽의 정상 점까지 잘려나간다.
        keep2 = ((ratio > med_r * (1.0 - halfw)) &
                 (ratio < float(np.percentile(ratio, 85)) * (1.0 + halfw)))
        if keep2.sum() < 10:
            break
        try:
            (ecx, ecy), (ew, eh), eang = cv2.fitEllipse(P[keep2].astype(np.float32))
        except cv2.error:
            break
        major, minor, theta_deg = split(ew, eh, eang)
        if major <= 0 or minor <= 0:
            break
        iou = fit_score(ecx, ecy, major, minor, theta_deg)
        if iou > best[0]:         # IoU 는 클수록 좋다 (sc 는 축소 배율이라 쓰면 안 된다)
            best = (iou, ecx, ecy, major, minor, theta_deg)

    # 두 번째 추정기: 실루엣 볼록껍질에 타원을 직접 피팅한다.
    # 광선 스캔은 잘린 원판(함정 7)에 강하지만 경계가 지저분하면 무너진다.
    # 껍질 피팅은 그 반대다. 서로 독립이라 둘 중 IoU 가 높은 쪽을 고른다.
    # 단 실루엣이 진짜 원판일 때만 쓴다. 프레임에 잘렸거나 (케이스가 들러붙어)
    # 오목하면 껍질이 엉뚱하게 부풀어 오히려 타원이 커진다 (실측 e/s 1.45).
    if (not clipped and sil_sol >= 0.93 and bool(params.get("hull_fit", True))):
        _cs2, _h2 = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if _cs2:
            hull2 = cv2.convexHull(max(_cs2, key=cv2.contourArea))
            if len(hull2) >= 5:
                try:
                    (hx, hy), (hw_, hh_), hang = cv2.fitEllipse(hull2)
                    hmaj, hmin, hth = split(hw_, hh_, hang)
                    if hmaj > 0 and hmin > 0:
                        hiou = fit_score(hx, hy, hmaj, hmin, hth)
                        if hiou > best[0]:
                            best = (hiou, hx, hy, hmaj, hmin, hth)
                except cv2.error:
                    pass

    _iou, ecx, ecy, major, minor, theta_deg = best

    inv = 1.0 / sc
    w = Wafer(
        found=True,
        center_px=(ecx * inv, ecy * inv),
        major_px=major * inv,
        minor_px=minor * inv,
        axis_ratio=float(minor / major),
        theta_deg=float(theta_deg % 180.0),
        clipped_by_frame=clipped,
    )
    w.tilt_deg = math.degrees(math.acos(max(0.0, min(1.0, w.axis_ratio))))
    w.mm_per_px = float(params["wafer_diameter_mm"]) / w.major_px
    # 플랫은 전체 해상도에서 직선 변을 직접 찾는다 (아래 _find_flat 설명 참고)
    line, edges = _find_flat(bgr, m, w, params)
    w.flat_line_px = line
    w.straight_edges_px = edges
    if line is not None:
        mx = (line[0][0] + line[1][0]) / 2.0
        my = (line[0][1] + line[1][1]) / 2.0
        w.flat_angle_deg = math.degrees(math.atan2(my - w.center_px[1],
                                                   mx - w.center_px[0])) % 360.0
    # 윤곽 신뢰도 지표. 광선을 많이 버렸거나(화면 밖) 타원이 실루엣보다
    # 뚜렷하게 크면 스케일이 통째로 틀어져 좌표가 몇십 % 어긋난다.
    w.ray_coverage = len(pts) / 360.0
    w.silhouette_solidity = sil_sol
    sil_area = float((m > 0).sum())
    w.ellipse_over_silhouette = (math.pi * major * minor / 4.0 / sil_area) if sil_area else 0.0
    return w, full


def _ell_radius(w, x, y):
    """(x,y) 방향에서의 피팅 타원 반지름 (전체 해상도 픽셀)."""
    t = math.atan2(y - w.center_px[1], x - w.center_px[0]) - math.radians(w.theta_deg)
    a, b = w.major_px / 2.0, w.minor_px / 2.0
    if a <= 0 or b <= 0:
        return 1e-6
    return 1.0 / math.sqrt((math.cos(t) / a) ** 2 + (math.sin(t) / b) ** 2)


def _find_flat(bgr, small_mask, w, params):
    """플랫을 '원판 실루엣의 긴 직선 변' 으로 찾는다.

    예전 방식(경계점이 타원 반지름의 93% 미만인 방향의 평균)은 못 쓴다.
    4인치 표준 플랫(32.5mm)은 가장 깊은 곳이 반지름의 94.6% 라 한 점도 통과
    못 하고, 대신 테두리 가까이 놓인 어두운 샘플이 원판 마스크에 판 홈
    (0.8~0.9) 만 잡혀 FLAT 이 엉뚱한 샘플을 가리켰다.

    돌려주는 값: (flat_line_px 또는 None, straight_edges_px 목록) - 전체 해상도.
    """
    H, W = bgr.shape[:2]
    r = w.major_px / 2.0
    if r <= 0:
        return None, []
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    S, V = hsv[:, :, 1], hsv[:, :, 2]
    m = ((S < int(params.get("sat_max", SAT_MAX))) &
         (V > int(params.get("val_min", VAL_MIN))) &
         (V < int(params.get("val_max", VAL_MAX)))).astype(np.uint8) * 255
    # 축소본 실루엣을 조금 부풀린 영역 안으로 한정 (바깥의 저채도 배경 차단)
    sil = cv2.resize(small_mask, (W, H), interpolation=cv2.INTER_NEAREST)
    m = cv2.bitwise_and(m, cv2.dilate(sil, _disk(max(1, 0.02 * r))))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, _disk(2))
    m = _largest_component(m)
    if m is None:
        return None, []
    m = _fill_holes(m)
    # 반지름 25% 디스크로 CLOSE: 테두리 샘플이 판 홈은 메워지고(좁고 깊다),
    # 플랫은 얕고 넓어 안 메워진다.
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, _disk(0.25 * r))

    cs, _h = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None, []
    cnt = max(cs, key=cv2.contourArea)
    hull = cv2.convexHull(cnt)
    ap = cv2.approxPolyDP(cnt, max(1.0, 0.005 * r), True).reshape(-1, 2).astype(float)
    if len(ap) < 2:
        return None, []

    cands = []
    for i in range(len(ap)):
        p1 = ap[i]
        p2 = ap[(i + 1) % len(ap)]
        L = float(math.hypot(p2[0] - p1[0], p2[1] - p1[1]))
        if not (0.28 * r <= L <= 1.2 * r):
            continue
        # 프레임에 잘린 변은 진짜 변이 아니라 화면 경계선이다
        if min(p1[0], p2[0], p1[1], p2[1]) < 3 or            max(p1[0], p2[0]) > W - 4 or max(p1[1], p2[1]) > H - 4:
            continue
        mx, my = (p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0
        rad = math.hypot(mx - w.center_px[0], my - w.center_px[1])
        er = _ell_radius(w, mx, my)
        # 케이스 반사광이 붙어 만든 직선은 타원 밖이라 여기서 걸린다
        if not (0.85 <= rad / er <= 1.03):
            continue
        # 홈 바닥(= 샘플이 판 자리)의 안쪽 변은 볼록껍질에서 멀리 떨어져 있다
        if abs(cv2.pointPolygonTest(hull, (float(mx), float(my)), True)) > 0.02 * r:
            continue
        cands.append((L, [[float(p1[0]), float(p1[1])], [float(p2[0]), float(p2[1])]]))

    # 그늘로 깎인 자리도 0.3r 남짓한 직선을 만든다 -> 0.45r 이상만 신뢰
    edges = [c[1] for c in cands if c[0] >= 0.45 * r]
    if not edges:
        return None, []            # 잘못 가리키느니 안 가리키는 게 낫다
    best = max((c for c in cands if c[0] >= 0.45 * r), key=lambda c: c[0])
    return best[1], edges


# --------------------------------------------------------------------------
# 7-2. 엣지 기반 원판 찾기 (무채색 배경용 폴백)
# --------------------------------------------------------------------------
# 실제 사용 환경은 철판 위다. 철판은 웨이퍼와 똑같이 무채색이라
# (실측: 웨이퍼 S 22~32, 철판 S 20~29) 색 마스크가 철판 전체로 번져 타원이
# 엉터리로 잡힌다 (실측 tilt 54도, mm/px 0.085). 밝기(V)나 질감으로도 안
# 갈라진다 - 철판의 빛줄기/그림자 때문에 V 가 겹치고, 초점이 맞은 사진에서는
# 웨이퍼 무광 질감의 스펙클이 철판 스크래치만큼 텍스처 에너지를 낸다.
# 그래서 색·밝기 전제 없이 '원호냐' 라는 기하만으로 찾는 경로를 따로 둔다.
EDGE_W = 640          # 엣지 탐색 축소 폭
# 둘레 표본이 엣지에 '얹혔다' 고 보는 거리. 2.0px 로 재면 검증이 끝난 나무 책상
# 장면(151738)의 색 경로 지지율이 0.219 로 나와 COLOR_KEEP_SUP(0.25) 아래로
# 떨어지고, 멀쩡한 색 타원(tilt 19도)이 엣지 타원(tilt 9도)으로 갈아타면서
# 검출 수가 16 -> 18 로 틀어졌다. 3.0px 로 재면 실측 분포가 제자리로 온다:
# 나무 책상 0.37/0.63/0.88, 철판 0.00/0.03/0.05, 대각선 사진 0.17.
EDGE_SUP_PX = 3.0
EDGE_MIN_SUP = 0.30   # 이 아래는 후보로도 안 본다
EDGE_TAKE_SUP = 0.45  # (구) 엣지 경로 전환 문턱. 아래 후보 경쟁 구조로 대체됨
COLOR_KEEP_SUP = 0.55  # (구) 색 경로 유지 문턱. 아래 후보 경쟁 구조로 대체됨.
# 지지율을 약한 엣지 맵으로 재면서 값이 전체적으로 올라갔다. 실측 분포:
#   색 경로가 정상인 장면(나무 책상)          s1 = 0.69 ~ 0.96
#   색 경로가 깨진 장면(철판/거울/검은판/대각선) s1 = 0.00 ~ 0.45
# 주의: 약한 맵에는 잡티가 많아 '엉뚱한 타원' 도 0.7 대 지지율이 나온다
# (실측: 140908 의 엣지 후보가 그랬다). 그래서 s2 가 높다고 갈아타면 안 되고,
# 색 경로 s1 이 낮을 때만 엣지 경로를 본다.


# 채도 상한을 하나로 고정하는 것은 원리적으로 불가능하다: 35 면 코팅된 초록
# 웨이퍼(실측 S 42~46)를 통째로 놓치고, 55 로 올리면 투명 케이스 테두리(S 46)가
# 원판에 들러붙는다 (예전에 실패한 방식). 그래서 고정 임계 대신 여러 후보를
# 만들어 경쟁시키고, '엣지에 얼마나 얹혔나(지지율)' 와 '안팎이 다른 재질인가
# (링 대비)' 를 곱한 점수로 고른다.
RING_FULL = 20.0          # Lab 거리. 이만큼이면 '재질 경계' 만점
MIN_FILL = 0.25           # 100mm 원판이 화면 최소변의 이보다 작게 찍힐 리 없다
ACCEPT_SCORE = 0.35       # 이 아래는 원판으로 인정하지 않는다
EDGE_WIN_MARGIN = 0.15    # 엣지가 색보다 이만큼 나아야 갈아탄다
COLOR_SWEEP_MARGIN = 0.20  # 설정된 sat_max 를 버릴 만큼 나아야 훑은 값을 쓴다


def _edge_ctx(bgr):
    """(강한 엣지, 거리맵, 약한 엣지, 축소배율) - 두 경로가 같이 쓴다.

    검은 무광 판을 대고 거울면 웨이퍼를 찍으면 웨이퍼와 철판이 둘 다 밝아져
    테두리 대비가 약해진다. 강한 엣지(40,120)에는 테두리가 위아래 호 두 조각만
    남아 진짜 원이 후보에 못 들어가거나 지지율이 EDGE_MIN_SUP 아래로 떨어져
    엣지 경로가 통째로 기권했다 (실측 144736). 그래서 약한 엣지(15,45)를 같이
    만들고, 채점(거리맵)은 약한 쪽으로 한다. 약한 맵의 철판 잡티는 방향이
    제각각이라 타원 둘레 360표본에 일관되게 못 올라탄다.
    """
    H, W = bgr.shape[:2]
    sc = float(EDGE_W) / W
    small = cv2.resize(bgr, (EDGE_W, max(1, int(round(H * sc)))),
                       interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (0, 0), 1.5)
    strong = cv2.Canny(g, 40, 120)
    weak = cv2.Canny(g, 15, 45)
    dt = cv2.distanceTransform(cv2.bitwise_not(weak), cv2.DIST_L2, 5)
    return strong, dt, weak, sc


def _support(dt, cx, cy, major, minor, ang):
    """둘레 지지율: 타원 둘레 360표본 중 엣지에서 2px 이내인 비율.

    철판의 스크래치·빛줄기·그림자 경계는 직선이라 타원 둘레에 못 올라타고,
    웨이퍼 테두리는 원호라 뚜렷하게 갈린다. 화면 안 표본이 150개 미만이면
    (프레임 밖으로 크게 벗어난 타원) 후보로 안 친다.
    """
    h, w = dt.shape
    t = np.linspace(0, 2 * math.pi, 360, endpoint=False)
    a, b = major / 2.0, minor / 2.0
    ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))
    xs = cx + a * np.cos(t) * ca - b * np.sin(t) * sa
    ys = cy + a * np.cos(t) * sa + b * np.sin(t) * ca
    xi, yi = np.round(xs).astype(int), np.round(ys).astype(int)
    ok = (xi >= 0) & (yi >= 0) & (xi < w) & (yi < h)
    if int(ok.sum()) < 150:
        return 0.0
    return float((dt[yi[ok], xi[ok]] < EDGE_SUP_PX).mean())


def _split_ellipse(ew, eh, eang):
    """fitEllipse 의 (w,h,angle) 을 (장축, 단축, 장축방향) 으로."""
    return (ew, eh, eang) if ew >= eh else (eh, ew, eang + 90.0)


def _fit_from_edges(ctx):
    """엣지 조각들로 타원을 찾아 ((cx,cy),(major,minor),ang) 과 지지율을 낸다.

    조각 하나(원호 60도 남짓)만으로는 fitEllipse 가 자주 발산해서 쌍 조합이
    필수다 (실측: 단독 창만 쓰면 142127 에서 엉뚱한 타원이 나왔다).
    """
    strong, dt, weak, sc = ctx
    h, w = strong.shape

    def windows(edges, cap):
        """길이 60px 이상 윤곽을 120px 창(스텝 60)으로 자른 원호 조각들."""
        out = []
        cs, _hh = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        for c in sorted(cs, key=len, reverse=True):
            pts = c.reshape(-1, 2)
            if len(pts) < 60:
                continue
            for st in range(0, max(1, len(pts) - 60), 60):
                out.append(pts[st:st + 120].astype(np.float32))
                if len(out) >= cap:
                    return out
        return out

    # 대비가 약한 테두리는 강한 맵에 호 두 조각만 남아 진짜 원이 후보에 아예
    # 안 들어간다 (실측 144736: 눌린 엉뚱한 타원이 뽑혔다). 약한 맵의 잡티
    # 조각도 같이 넣는다 - 피팅해 봐야 지지율이 낮아 알아서 진다.
    segs = windows(strong, 60) + windows(weak, 60)
    if not segs:
        return None, 0.0

    lo, hi = 0.35 * min(h, w), 1.35 * min(h, w)

    def try_fit(pts):
        if len(pts) < 5:
            return None
        try:
            (cx, cy), (ew, eh), ang = cv2.fitEllipse(pts)
        except cv2.error:
            return None
        major, minor, th = _split_ellipse(ew, eh, ang)
        if not (lo <= major <= hi) or major <= 0 or minor / major < 0.55:
            return None
        sup = _support(dt, cx, cy, major, minor, th)
        return (sup, cx, cy, major, minor, th)

    best = None
    for i in range(len(segs)):
        # 창이 120개면 조합이 7천 개라, 이미 확실한 답이 나왔으면 멈춘다
        # (없으면 1초를 넘긴다).
        if best is not None and best[0] > 0.90:
            break
        for j in range(i, len(segs)):
            r = try_fit(segs[i] if i == j else np.vstack([segs[i], segs[j]]))
            if r and (best is None or r[0] > best[0]):
                best = r
                if best[0] > 0.90:
                    break
    if best is None or best[0] <= EDGE_MIN_SUP:
        return None, 0.0

    # 정제: 그 타원의 링 3px 안에 있는 엣지 픽셀을 전부 모아 재피팅.
    # 지지율이 좋아질 때만 채택한다 (나빠지면 잡음을 끌어안은 것).
    ey, ex = np.nonzero(weak)
    exf, eyf = ex.astype(np.float32), ey.astype(np.float32)
    for _ in range(3):
        _sup, cx, cy, major, minor, th = best
        t = math.radians(th)
        dx, dy = exf - cx, eyf - cy
        u = dx * math.cos(t) + dy * math.sin(t)
        v = -dx * math.sin(t) + dy * math.cos(t)
        a, b = major / 2.0, minor / 2.0
        rho = np.sqrt((u / a) ** 2 + (v / b) ** 2)
        sel = np.abs(rho - 1.0) * ((a + b) / 2.0) < 3.0
        if int(sel.sum()) < 5:
            break
        r = try_fit(np.stack([exf[sel], eyf[sel]], 1))
        if not r or r[0] <= best[0]:
            break
        best = r

    sup, cx, cy, major, minor, th = best
    inv = 1.0 / sc
    return ((cx * inv, cy * inv), (major * inv, minor * inv), th), sup


def find_wafer_edges(bgr, params):
    """엣지만으로 원판 타원을 찾는다. ((cx,cy),(major,minor),ang), 지지율."""
    return _fit_from_edges(_edge_ctx(bgr))


def _ring_contrast(bgr, cx, cy, major, minor, th):
    """타원 안쪽 띠와 바깥쪽 띠의 Lab 중앙값 거리.

    '엣지가 있다' 와 '재질 경계다' 는 다르다. 케이스 테두리나 모서리 반사광 원은
    테두리가 깔끔해 지지율이 높지만 안팎이 같은 재질이라 이 값이 낮다
    (실측: 진짜 원판 16~90, 가짜 7~14).
    """
    H, W = bgr.shape[:2]
    a, b = major / 2.0, minor / 2.0
    if a <= 0 or b <= 0:
        return 0.0
    x0, x1 = max(0, int(cx - 1.25 * a)), min(W, int(cx + 1.25 * a) + 1)
    y0, y1 = max(0, int(cy - 1.25 * a)), min(H, int(cy + 1.25 * a) + 1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0
    lab = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2LAB).astype(np.float32)
    yy, xx = np.indices((y1 - y0, x1 - x0), dtype=np.float32)
    dx, dy = xx + x0 - cx, yy + y0 - cy
    t = math.radians(th)
    u = dx * math.cos(t) + dy * math.sin(t)
    v = -dx * math.sin(t) + dy * math.cos(t)
    rho = np.sqrt((u / a) ** 2 + (v / b) ** 2)
    inb = (rho >= 0.80) & (rho <= 0.96)
    outb = (rho >= 1.04) & (rho <= 1.20)
    if int(inb.sum()) < 200 or int(outb.sum()) < 200:
        return 0.0
    mi = np.median(lab[inb], axis=0)
    mo = np.median(lab[outb], axis=0)
    return float(np.linalg.norm(mi - mo))


def _cand_score(sup, ring):
    """지지율 x 재질 경계 가중치. 둘 다 있어야 진짜 원판이다."""
    return sup * (0.5 + 0.5 * min(1.0, ring / RING_FULL))


def _plausible_disc(bgr, params):
    """이 채도 상한에서 만들어지는 무채색 덩어리가 '원판 크기' 인지 값싸게 본다."""
    H, W = bgr.shape[:2]
    sc = 480.0 / W
    small = cv2.resize(bgr, (480, max(1, int(round(H * sc)))), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    S, V = hsv[:, :, 1], hsv[:, :, 2]
    m = ((S < int(params.get("sat_max", SAT_MAX))) &
         (V > int(params.get("val_min", VAL_MIN))) &
         (V < int(params.get("val_max", VAL_MAX)))).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, _disk(6))
    m = _largest_component(m)
    if m is None:
        return False
    return float((m > 0).sum()) / m.size <= 0.55


def find_wafer_best(bgr, params):
    """색 후보(채도 상한 여러 개)와 엣지 후보를 경쟁시켜 원판을 고른다.

    (Wafer, 실루엣 마스크, 추가 경고 목록) 을 돌려준다.
    """
    H, W = bgr.shape[:2]
    ctx = _edge_ctx(bgr)
    sc = ctx[3]
    min_major = MIN_FILL * min(H, W)

    def rate(w):
        sup = _support(ctx[1], w.center_px[0] * sc, w.center_px[1] * sc,
                       w.major_px * sc, w.minor_px * sc, w.theta_deg)
        ring = _ring_contrast(bgr, w.center_px[0], w.center_px[1],
                              w.major_px, w.minor_px, w.theta_deg)
        return sup, ring, _cand_score(sup, ring)

    # ---- 색 후보: 채도 상한을 훑는다 -----------------------------------
    colors = []
    for sat in sorted({int(params.get("sat_max", SAT_MAX)), 45, 55, 65}):
        q = dict(params)
        q["sat_max"] = sat
        # 상한을 올리다 보면 어느 순간 마스크가 장면 전체로 번진다. 그때
        # find_wafer 의 광선 스캔이 화면 끝까지 훑느라 100초를 넘긴다 (실측
        # 144712, sat=65: 110초 / 장축 8000px). 어차피 뒤에서 탈락할 후보이므로,
        # 값싼 축소 마스크로 미리 재서 프레임의 55% 를 넘으면 건너뛴다
        # (100mm 원판이 화면 최소변을 꽉 채워도 44% 다).
        if not _plausible_disc(bgr, q):
            continue
        w, m = find_wafer(bgr, q)
        if not w.found or w.major_px < min_major:
            continue
        # 원판은 정의상 타원 안이다. 케이스 모서리 반사가 마스크에 붙어 있으면
        # 타원 피팅은 견디지만 '프레임에 잘림'/'오목함' 경고가 헛되이 뜨고 ROI
        # 껍질이 부푼다. 그래서 실루엣을 타원 1.10 배로 잘라내고 그 마스크로
        # clipped/solidity 를 다시 잰다.
        lim = np.zeros((H, W), np.uint8)
        cv2.ellipse(lim, (int(round(w.center_px[0])), int(round(w.center_px[1]))),
                    (max(1, int(w.major_px / 2 * 1.10)), max(1, int(w.minor_px / 2 * 1.10))),
                    w.theta_deg, 0, 360, 255, -1)
        m = cv2.bitwise_and(m, lim)
        w.clipped_by_frame = _touches_border(m)
        cs, _hh = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cs:
            c = max(cs, key=cv2.contourArea)
            ha = cv2.contourArea(cv2.convexHull(c))
            w.silhouette_solidity = float(cv2.contourArea(c) / ha) if ha > 0 else 0.0
        w.sat_max_used = sat
        w.outline_source = "color"
        sup, ring, score = rate(w)
        w.outline_support = sup
        colors.append({"w": w, "m": m, "sat": sat, "sup": sup, "ring": ring, "score": score})

    # ---- 엣지 후보 ------------------------------------------------------
    edge = None
    ell, s2 = _fit_from_edges(ctx)
    if ell is not None:
        (cx, cy), (major, minor), th = ell
        if major >= min_major:
            w = Wafer(found=True, center_px=(cx, cy), major_px=major, minor_px=minor,
                      axis_ratio=float(minor / major), theta_deg=float(th % 180.0))
            w.tilt_deg = math.degrees(math.acos(max(0.0, min(1.0, w.axis_ratio))))
            w.mm_per_px = float(params["wafer_diameter_mm"]) / w.major_px
            # 실루엣은 타원을 채운 합성 마스크다. 그래서 볼록도/면적비는 정의상
            # 1.0, 광선 스캔은 아예 쓰지 않았으므로 커버리지도 1.0 으로 둔다
            # (0 으로 두면 '광선 0% 사용' 이라는 엉뚱한 경고가 뜬다).
            mask = np.zeros((H, W), np.uint8)
            cv2.ellipse(mask, (int(round(cx)), int(round(cy))),
                        (max(1, int(major / 2)), max(1, int(minor / 2))),
                        w.theta_deg, 0, 360, 255, -1)
            w.clipped_by_frame = _touches_border(mask)
            w.silhouette_solidity = 1.0
            w.ellipse_over_silhouette = 1.0
            w.ray_coverage = 1.0
            # 색 마스크가 무의미한 장면이므로 플랫/직선 변은 아예 찾지 않는다.
            # 엉뚱한 플랫을 그리느니 안 그리는 게 낫다.
            w.flat_angle_deg = None
            w.flat_line_px = None
            w.straight_edges_px = []
            w.sat_max_used = 0
            w.outline_source = "edge"
            w.outline_support = s2
            ring = _ring_contrast(bgr, cx, cy, major, minor, w.theta_deg)
            edge = {"w": w, "m": mask, "sup": s2, "ring": ring,
                    "score": _cand_score(s2, ring)}

    # ---- 고르기 (순서를 바꾸면 회귀가 난다) ------------------------------
    warns = []
    best_color = max(colors, key=lambda c: c["score"]) if colors else None
    pick = None
    if best_color is None:
        pick = edge
    elif edge is not None and edge["score"] > best_color["score"] + EDGE_WIN_MARGIN:
        pick = edge
    else:
        # 색 경로로 갈 때만: 설정된 sat_max 후보가 최고와 큰 차이가 없으면
        # 그것을 쓴다. 판정은 이미 최고 색 점수로 끝났으므로 여기서 낮춰도
        # 엣지 선택에 영향을 주지 않는다 (이 순서가 아니면 기존 장면이 엣지로
        # 넘어간다).
        setv = int(params.get("sat_max", SAT_MAX))
        same = [c for c in colors if c["sat"] == setv]
        pick = best_color
        if same and same[0]["score"] >= best_color["score"] - COLOR_SWEEP_MARGIN:
            pick = same[0]

    if pick is None:
        w = Wafer()
        w.outline_source = "color"
        return w, np.zeros((H, W), np.uint8), []

    if pick is edge:
        warns.append("wafer outline fitted from edges; flat not detectable")
    if pick["score"] < ACCEPT_SCORE:
        pick["w"].found = False
        warns.append("wafer outline weak (support %.0f%%, edge contrast %.0f)"
                     % (pick["sup"] * 100, pick["ring"]))
    return pick["w"], pick["m"], warns


def unproject_matrix(w):
    """기울어진 촬영의 역사영 행렬.

    장축은 기울기 영향을 안 받고 단축만 cos(tilt) 배로 줄어든다. 그래서
    장축 방향으로 회전 -> 단축만 major/minor 배 늘림 -> 되돌린다.
    """
    th = math.radians(w.theta_deg)
    R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    k = (w.major_px / w.minor_px) if w.minor_px > 0 else 1.0
    return R @ np.diag([1.0, k]) @ R.T


# --------------------------------------------------------------------------
# 7-4. 조명 얼룩 평탄화
# --------------------------------------------------------------------------
def _local_bg(ch, roi, r_px):
    """한 채널의 국소 배경을 추정한다 (_flatten 과 _color_distance 가 함께 쓴다).

    ROI 바깥을 '안쪽 중앙값' 으로 덮으면, 케이스 반사광/그늘 때문에 중앙값과
    10~20% 다른 테두리 쪽 웨이퍼가 배경을 끌어당겨 테두리 안쪽에 폭 10mm 짜리
    가짜 편차 띠(halo)가 생긴다. 그 띠가 grow 임계를 넘어 옆 샘플과 한
    연결요소로 묶여 샘플까지 사라졌다 (실측 165x63px, 297mm^2 덩어리).
    그래서 바깥 픽셀마다 '가장 가까운 안쪽 픽셀의 값' 을 이어 붙인다
    (Voronoi 채우기). 값이 테두리 밖으로 연속되므로 halo 가 없다.

    함정 3: 커널이 샘플 크기와 비슷하면 배경이 샘플을 따라가 나눗셈 후 샘플이
    통째로 사라진다. 그래서 웨이퍼 반지름이 60px 이 되도록 축소한 뒤 21 커널.
    """
    g = ch.astype(np.float32)
    inside = roi > 0
    if not inside.any():
        filled = np.clip(g, 0, 255).astype(np.uint8)
    else:
        _d, lbl = cv2.distanceTransformWithLabels(
            (~inside).astype(np.uint8), cv2.DIST_L2, 5,
            labelType=cv2.DIST_LABEL_PIXEL)
        ys, xs = np.nonzero(inside)
        lut = np.zeros(int(lbl.max()) + 1, np.float32)
        lut[lbl[ys, xs]] = g[ys, xs]
        filled = lut[lbl].astype(np.uint8)

    ds = max(1, int(round(r_px / 60.0)))
    h, w = filled.shape
    small = cv2.resize(filled, (max(24, w // ds), max(24, h // ds)), interpolation=cv2.INTER_AREA)
    # 평균이 아니라 중앙값: 샘플(이상치)이 배경 추정을 끌어당기지 않는다.
    small = cv2.medianBlur(small, 21)
    small = cv2.GaussianBlur(small.astype(np.float32), (0, 0), 2)  # 블록감 제거
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def _flatten(gray, roi, r_px):
    """중앙값 배경으로 나눠 편차(%) 맵을 만든다.

    함정 5: top-hat/black-hat 은 무광 질감 노이즈를 그대로 집어내 더 나빴다.
    """
    g = gray.astype(np.float32)
    bg = np.maximum(_local_bg(gray, roi, r_px), 1.0)
    flat = g / bg
    return np.abs(flat - 1.0) * 100.0


def _color_distance(bgr, roi, r_px):
    """Lab 세 채널 각각 '국소 배경과의 차' 를 재서 색거리(dE) 맵을 만든다.

    밝기(_flatten)와 채도(sat_delta)로는 안 갈라지는, '색조만 다른' 조각을
    잡기 위한 축이다. 배경 추정이 같은 방식이라 조명 얼룩에는 둔감하다.
    """
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    acc = np.zeros(lab.shape[:2], np.float32)
    for i in range(3):
        d = lab[:, :, i].astype(np.float32) - _local_bg(lab[:, :, i], roi, r_px)
        acc += d * d
    return np.sqrt(acc)


# --------------------------------------------------------------------------
# 메인 검출
# --------------------------------------------------------------------------
def detect(bgr, params=None):
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    t0 = cv2.getTickCount()
    res = DetectResult()
    H, W = bgr.shape[:2]
    res.mask = np.zeros((H, W), np.uint8)
    res.dev_img = np.zeros((H, W), np.uint8)
    res.roi = np.zeros((H, W), np.uint8)

    def done():
        res.detect_ms = (cv2.getTickCount() - t0) / cv2.getTickFrequency() * 1000.0
        return res

    # ---- 7-2.5 표면 모드: 스케일과 원점을 어디서 가져올지 정한다 ----------
    surface = str(p.get("surface", "auto")).lower()
    plate_mm = float(p.get("plate_mm_per_px", 0) or 0)
    mk_mm, res.markers, res.marker_polys, mk_warn = find_markers(bgr, p)
    res.warnings.extend(mk_warn)
    plate = (surface == "plate")
    wafer, wmask = Wafer(), np.zeros((H, W), np.uint8)

    if not plate:
        wafer, wmask, wwarn = find_wafer_best(bgr, p)
        res.warnings.extend(wwarn)
        if surface == "auto":
            # 크기 앵커의 기준 스케일은 마커뿐이다. 앵커는 '이 사진에서 100mm 가
            # 몇 픽셀인가' 를 알아야 성립하는데, 마커는 촬영마다 그 자리에서 재므로
            # 높이가 바뀌어도 맞지만 plate_mm_per_px 는 과거 한 시점의 높이를
            # 가정한 상수라 촬영 조건이 바뀌면 진짜 웨이퍼를 기각한다
            # (실측 105533: 575px 실측 vs 398px 기대 -> 철판 모드로 떨어져 검출
            # 13개가 전부 책상 나뭇결/케이스 잡티였다).
            # 그래서 plate_mm_per_px 는 '철판 모드의 스케일 폴백' 으로만 쓰고
            # 게이트에는 쓰지 않는다.
            anchor_mm = mk_mm or 0.0
            if anchor_mm > 0 and wafer.found:
                # 100mm 원판이면 장축 픽셀 수가 정해진다. 잡티로 만들어진 가짜
                # 타원은 여기서 걸린다.
                exp = float(p["wafer_diameter_mm"]) / anchor_mm
                if abs(wafer.major_px - exp) > 0.15 * exp:
                    res.warnings.append(
                        "wafer-like ellipse rejected by size gate "
                        "(%.0f px vs expected %.0f px); plate mode"
                        % (wafer.major_px, exp))
                    plate = True
            elif anchor_mm <= 0 and not wafer.found:
                # 앵커가 없으면 '못 찾았을 때만' 철판으로 넘어간다 (기본값 조합에서
                # 지금까지의 동작이 그대로 유지되도록).
                res.warnings.append("no wafer found; plate mode")
                plate = True
        if plate:
            wafer, wmask = Wafer(), np.zeros((H, W), np.uint8)

    res.wafer = wafer
    res.wafer_mask = wmask

    if plate:
        # ---- 철판 모드: ROI = 화면에서 여백만큼 들인 직사각형 ---------------
        wafer.surface = "plate"
        res.warnings.append("plate mode: origin = frame center")
        base = float(min(H, W))
        m_px = int(round(base * float(p["plate_margin_pct"]) / 100.0))
        roi = np.zeros((H, W), np.uint8)
        roi[m_px:H - m_px, m_px:W - m_px] = 255
        # r_px 대용값. 배경 축소 배율, OPEN/CLOSE 커널, 테두리 띠 폭, 번호
        # 매기기 행 높이 등 r_px 에 비례하던 것들이 전부 이 값으로 돈다.
        r_px = base / 2.0
        # 스케일 우선순위: 마커 > 설정 보정값 > 없음(픽셀 단위)
        if mk_mm:
            wafer.scale_source = "marker"
            wafer.mm_per_px = mk_mm
            mm = mk_mm
        elif plate_mm > 0:
            wafer.scale_source = "settings"
            wafer.mm_per_px = plate_mm
            mm = plate_mm
        else:
            wafer.scale_source = "none"
            wafer.mm_per_px = 0.0           # 0 = '미보정' 이라는 뜻으로 남긴다
            mm = 1.0
            res.warnings.append("plate scale not calibrated; coordinates are in PIXELS")
        M = np.eye(2)                       # 기울기 보정 근거가 없다 (원판이 없음)
        cx0, cy0 = W / 2.0, H / 2.0         # 원점 = 화면 중심
        res.roi = roi
    else:
        if not wafer.found:
            res.warnings.append("wafer not found")
            return done()
        # 원판을 찾았는데 보정값이 없으면, 지금 값을 그대로 복사해 쓰라고 알려준다
        if plate_mm <= 0:
            res.info.append("hint: set plate_mm_per_px=%.5f to enable plate mode "
                            "at this camera height" % wafer.mm_per_px)

        r_px = wafer.major_px / 2.0
        mm = wafer.mm_per_px
        wafer.scale_source = "wafer"
        # 마커가 같이 보이면 서로 검산한다. 5% 넘게 어긋나면 둘 중 하나가
        # 틀린 것이므로 (원판 오검출이거나 마커가 구겨졌거나) 알린다.
        if mk_mm and mm > 0 and abs(mm - mk_mm) > 0.05 * mk_mm:
            res.warnings.append("wafer scale and marker scale disagree (%.0f%%)"
                                % (abs(mm - mk_mm) / mk_mm * 100.0))
        M = unproject_matrix(wafer)
        cx0, cy0 = wafer.center_px

        # ---- 7-3. ROI = 축소한 타원 ∩ 침식한 실제 실루엣 -------------------
        # 이상적인 타원만 쓰면 플랫 쪽에서 원판 밖으로 삐져나가 링 잡티가 생긴다.
        shrink = 1.0 - float(p["edge_margin_pct"]) / 100.0
        cx_c, cy_c = wafer.center_px
        ell = np.zeros((H, W), np.uint8)
        cv2.ellipse(ell, (int(round(wafer.center_px[0])), int(round(wafer.center_px[1]))),
                    (max(1, int(wafer.major_px / 2 * shrink)),
                     max(1, int(wafer.minor_px / 2 * shrink))),
                    wafer.theta_deg, 0, 360, 255, -1)
        # 실루엣 그대로가 아니라 '볼록 껍질' 과 교집합한다.
        # 원판은 플랫이 있어도 볼록하다 (직선으로 자른 것은 볼록성을 깨지 않는다).
        # 반면 앞을 가로지르는 케이스 벽이나 반사광은 실루엣에 홈을 내고, 그 홈이
        # 그대로 ROI 에 뚫려 안에 있던 샘플이 통째로 빠졌다 (실촬영 14개 중 4개).
        hull_mask = wmask
        _cs, _h = cv2.findContours(wmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if _cs:
            hull_mask = np.zeros_like(wmask)
            cv2.fillConvexPoly(hull_mask, cv2.convexHull(max(_cs, key=cv2.contourArea)), 255)
        sil = cv2.erode(hull_mask, _disk(max(1, r_px * float(p["edge_band_pct"]) / 100.0)))
        roi = cv2.bitwise_and(ell, sil)
        # 케이스 반사광이 플랫 끝에 붙으면 볼록껍질이 플랫 위로 들려서, 그 틈으로
        # 책상이 ROI 안에 들어온다. 찾아 둔 직선 변마다 반평면 컷을 한 번 더 한다
        # (직선 바깥쪽을 버리고 안쪽으로 r x edge_margin_pct/100 만큼 마진).
        if wafer.straight_edges_px:
            yy, xx = np.indices((H, W), dtype=np.float32)
            marg = r_px * float(p["edge_margin_pct"]) / 100.0
            for (ax, ay), (bx, by) in wafer.straight_edges_px:
                nx, ny = -(by - ay), (bx - ax)
                ln = math.hypot(nx, ny)
                if ln < 1e-6:
                    continue
                nx, ny = nx / ln, ny / ln
                if nx * (cx_c - ax) + ny * (cy_c - ay) < 0:   # 법선이 중심을 향하게
                    nx, ny = -nx, -ny
                keep = (nx * (xx - ax) + ny * (yy - ay)) >= marg
                roi = cv2.bitwise_and(roi, keep.astype(np.uint8) * 255)
        res.roi = roi
        if not roi.any():
            res.warnings.append("wafer not found")
            return done()

    # 마커 자체가 '샘플' 로 잡히면 안 된다. 사각형을 20% 부풀려 ROI 에서 뺀다
    # (검출 사각형이 실제 종이보다 조금 작게 잡히고, 인쇄물 테두리도 걸린다).
    if res.marker_polys:
        cut = np.zeros((H, W), np.uint8)
        for q in res.marker_polys:
            c = q.mean(axis=0)
            cv2.fillConvexPoly(cut, np.int32(c + (q - c) * 1.2), 255)
        roi = cv2.bitwise_and(roi, cv2.bitwise_not(cut))
        res.roi = roi

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    S, V = hsv[:, :, 1], hsv[:, :, 2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    roib = roi > 0
    # 설정값이 아니라 '색 경로가 실제로 채택한 상한' 으로 재야 한다. 코팅된
    # 초록 웨이퍼를 제대로 찾고도 설정값(35)으로 재면 9% 로 나와 "sensing area is
    # mostly not the wafer" 경고가 헛되이 떴다 (채택값 45 로 재면 0.96).
    _sm = int(wafer.sat_max_used) if wafer.sat_max_used else int(p.get("sat_max", SAT_MAX))
    wafer.roi_fit = float((((S < _sm) &
                            (V > int(p.get("val_min", VAL_MIN))))[roib]).mean())
    # 날아간 하이라이트 비율은 val_max 설정과 무관하게 항상 재야 진단이 된다.
    # 여기가 크면 웨이퍼와 샘플이 똑같이 255 라 대비가 0 이고, 자동초점도
    # 잡을 것이 없어 헌팅한다. 실측: 51% 인 사진에서 샘플 14개 중 4개를 놓쳤다.
    wafer.blown_pct = float((V[roib] >= BLOWN_LEVEL).mean() * 100.0)

    dev = _flatten(gray, roi, r_px)
    res.dev_img = np.clip(dev * 4.0, 0, 255).astype(np.uint8)

    # 금색·갈색 샘플은 밝기가 웨이퍼와 비슷해도 채도가 확실히 높다.
    # 반대 방향도 봐야 한다: 검은 판을 비춘 거울면 웨이퍼는 살짝 유채색이고
    # (실측 S 중앙값 42) 그 위의 회색 칩은 오히려 채도가 낮다 (S 27~35).
    # 저채도 쪽에는 V < 240 가드가 필수다. 안 그러면 날아간 하이라이트
    # (V>=250, S~0)가 통째로 '저채도 샘플' 로 걸린다.
    s_med = float(np.median(S[roib]))
    sdiff = S.astype(np.int16) - s_med
    sat_delta = float(p["sat_delta"])
    sat_mask = (sdiff > sat_delta) | ((-sdiff > sat_delta) & (V < 240))

    # ---- 7-5. 히스테리시스 임계화 -----------------------------------------
    # 함정 4: Otsu 는 클래스가 하나여도 반드시 둘로 쪼개 샘플을 반토막 낸다.
    seed_t = float(p["seed_pct"])
    grow_t = seed_t * float(p["grow_pct"]) / 100.0
    # 날아간 하이라이트(V>=250)는 대비 정보가 0 이라 샘플일 수 없는 곳이다.
    # 철판이 천장 조명을 정반사하면 폭 30~80px 포화 줄무늬가 편차 맵을 뒤덮어
    # 칩들이 그 덩어리에 흡수됐다 (실측 172657: 이 제외로 칩 검출 14 -> 25,
    # 중앙 무더기 전부 회수). 일부만 포화된 샘플은 나머지 부분으로 잡히고
    # 구멍은 뒤의 _fill_holes 가 메운다.
    notblown = V < 250
    seed = ((dev > seed_t) | sat_mask) & roib & notblown
    grow = (((dev > grow_t) | sat_mask) & roib & notblown).astype(np.uint8) * 255

    # 반지름 200px 근처에서 0.006*r 은 1px 이라 사실상 아무것도 안 한다.
    # 그 결과 무늬가 있는 샘플이 파편으로 쪼개져, 작은 조각은 면적 필터에
    # 걸려 사라지고 큰 조각 둘은 서로 다른 샘플로 이중 계수됐다.
    ko = max(1, int(round(float(p["open_pct"]) / 100.0 * r_px)))
    kc = max(1, int(round(float(p["close_pct"]) / 100.0 * r_px)))
    grow = cv2.morphologyEx(grow, cv2.MORPH_OPEN, _disk(ko))
    grow = cv2.morphologyEx(grow, cv2.MORPH_CLOSE, _disk(kc))
    grow = _fill_holes(grow)
    grow = cv2.bitwise_and(grow, roi)

    # 원판 테두리는 밝기가 급변해 항상 링 잡티를 만든다 -> band 에 닿으면 통째로 버림
    band_r = max(1, r_px * float(p["edge_band_pct"]) / 100.0)
    band = cv2.subtract(roi, cv2.erode(roi, _disk(band_r))) > 0

    min_a = float(p["min_area_mm2"])
    max_a = float(p["max_area_mm2"])
    edge_max = float(p["edge_touch_max"])
    split_sol = float(p["split_solidity"])

    def _mk(mask):
        """마스크 한 장을 후보 dict 로 만든다.

        이 단계부터 후보는 'lab==i 인덱스 목록' 이 아니라 자기 마스크(전체 크기)
        를 들고 다닌다. rescue/split 이 원래 연결요소에 없던 모양을 만들기 때문.
        """
        b = mask > 0
        pix = int(b.sum())
        if pix == 0:
            return None
        cs, _hh = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cs:
            return None
        pts = np.vstack(cs)
        ca = float(sum(cv2.contourArea(c) for c in cs))
        ha = cv2.contourArea(cv2.convexHull(pts))
        mo = cv2.moments(mask, binaryImage=True)
        if mo["m00"] == 0:
            return None
        return {"mask": mask, "pix": pix, "pts": pts, "ca": ca,
                "sol": (ca / ha) if ha > 0 else 0.0,
                "area": pix * mm * mm,
                "edge_frac": float(band[b].sum()) / pix,
                "dev": float(dev[b].mean()),
                "cx": mo["m10"] / mo["m00"], "cy": mo["m01"] / mo["m00"],
                "edge": False, "rescued": False, "split": False, "merged": False}

    def _ok(g, need_sol):
        """1단계와 같은 검사. 볼록도 문턱만 호출자가 정한다."""
        return (bool(seed[g["mask"] > 0].any()) and g["edge_frac"] <= edge_max
                and g["sol"] >= need_sol and min_a <= g["area"] <= max_a)

    n, lab, stats, _ = cv2.connectedComponentsWithStats((grow > 0).astype(np.uint8), 8)

    # ---- 1단계: 후보 성분 모으기 (면적/볼록도 판정은 병합 뒤에) ------------
    raw, rejects = [], []
    for i in range(1, n):
        if not seed[lab == i].any():
            continue                      # seed 를 하나도 안 품으면 잡티
        g = _mk((lab == i).astype(np.uint8) * 255)
        if g is None:
            continue
        # 함정 6: 꼭짓점 4개 조건은 쓰지 않는다. 삼각형/사다리꼴 샘플이 섞여 있다.
        # 볼록도는 '조각 단위' 로 여기서 본다. 병합 뒤에 보면, 한 샘플의 조각들을
        # 합친 결과(사이가 벌어져 있어 볼록도가 낮다)가 통째로 걸러져 버린다.
        # 테두리는 '닿으면 폐기' 가 아니라 '띠 안에 잠긴 비율' 로 판정한다.
        # 닿으면 폐기로 하면 띠를 모서리로 스친 멀쩡한 샘플까지 사라졌다.
        if g["edge_frac"] > edge_max or g["sol"] < 0.55 or g["area"] > max_a:
            if g["area"] >= min_a:
                rejects.append(g)         # 두꺼운 몸통은 아래 rescue 에서 건진다
            continue
        if g["edge_frac"] > 0 and g["sol"] < split_sol:
            continue                      # 테두리 반사광 파편 (실측 0.74)
        g["edge"] = g["edge_frac"] > 0
        raw.append(g)

    # ---- 1.5단계: 탈락한 덩어리에서 두꺼운 몸통 건지기 (rescue) ------------
    # 띠는 얇아서 OPEN 반지름을 키우면 먼저 떨어져 나가고 샘플 몸통은 남는다.
    # 첫 단계에서 바로 받으면 띠 자락이 붙은 채 통과해 면적이 30% 넘게 부풀고
    # 무게중심이 밀린다 (실측). 그래서 '면적이 안정되는 지점' 에서 받는다:
    # 다음 단계에서 면적이 8% 넘게 줄면 아직 띠가 붙은 것이라 넘어가고,
    # 8% 이내로만 줄면(모서리만 둥글어짐) 그 단계의 조각을 받는다.
    for g0 in rejects:
        levels = []
        for pct in (2, 3, 4, 5, 6, 8):
            op = cv2.morphologyEx(g0["mask"], cv2.MORPH_OPEN, _disk(r_px * pct / 100.0))
            nn, ll, st, _s = cv2.connectedComponentsWithStats((op > 0).astype(np.uint8), 8)
            levels.append([(ll == j) for j in range(1, nn)
                           if st[j, cv2.CC_STAT_AREA] * mm * mm >= min_a])
        taken = np.zeros(g0["mask"].shape, bool)
        for k in range(len(levels) - 1):
            for pc in levels[k]:
                if (pc & taken).any():
                    continue              # 이미 받아들인 조각과 겹치면 건너뛴다
                a0 = int(pc.sum())
                nxt = max(levels[k + 1], key=lambda q: int((q & pc).sum()), default=None)
                if nxt is None or not (nxt & pc).any():
                    continue
                if int(nxt.sum()) < 0.92 * a0:
                    continue              # 아직 띠가 붙어 있다
                g = _mk(pc.astype(np.uint8) * 255)
                if g is None or not _ok(g, split_sol):
                    continue
                taken |= pc
                g["rescued"] = True
                g["edge"] = g["edge_frac"] > 0
                raw.append(g)

    # ---- 2단계: 한 샘플이 쪼개진 조각들을 합친다 ---------------------------
    # 무늬(각인)나 밝기 기울기가 있는 샘플은 조각으로 갈라져서, 작은 조각은
    # 면적 필터에 걸려 사라지고 큰 조각 둘은 서로 다른 샘플로 이중 계수됐다.
    # 모폴로지 CLOSE 를 키우면 '이웃한 다른 샘플' 까지 붙어버려서 못 쓴다.
    #
    # 거리만 보고 합치면 잡티가 다리가 되어 멀리 있는 칩끼리 줄줄이 엮인다.
    # 그래서 '합친 결과가 여전히 볼록한가' 를 함께 본다. 한 샘플의 조각들은
    # 합쳐도 볼록하지만, 떨어진 두 샘플을 합치면 껍질만 커져 볼록도가 무너진다.
    # rescue 로 띠에서 일부러 떼어낸 조각은 병합에서 뺀다. 안 그러면 잡티
    # 파편을 도로 붙여 면적이 6% 부푼다 (실측).
    merge_px = float(p["merge_mm"]) / mm if mm > 0 else 0.0
    guard = float(p["merge_solidity"])

    items = list(raw)
    while True:
        best = None
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                if items[a]["rescued"] or items[b]["rescued"]:
                    continue
                d = math.hypot(items[a]["cx"] - items[b]["cx"],
                               items[a]["cy"] - items[b]["cy"])
                if d >= merge_px or (best and d >= best[0]):
                    continue
                u = _mk(cv2.bitwise_or(items[a]["mask"], items[b]["mask"]))
                if u is None or u["sol"] < guard:
                    continue
                u["edge"] = items[a]["edge"] or items[b]["edge"]
                u["merged"] = True
                best = (d, a, b, u)
        if best is None:
            break
        _d, a, b, u = best
        items = [g for i, g in enumerate(items) if i not in (a, b)] + [u]

    # ---- 2.5단계: 맞닿아 한 덩어리로 잡힌 샘플 둘 가르기 (split) -----------
    # 맞닿은 샘플 두 개는 처음부터 한 연결요소라 1개(실측 112mm^2)로 잡힌다.
    # 병합 로직과는 무관한 문제라, 외곽의 가장 깊은 홈 두 개를 이어 자른다.
    def _split(g):
        cs, _hh = cv2.findContours(g["mask"], cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cs:
            return None
        cnt = max(cs, key=cv2.contourArea)
        if len(cnt) < 5:
            return None
        hidx = cv2.convexHull(cnt, returnPoints=False)
        if hidx is None or len(hidx) < 4:
            return None
        try:
            dfs = cv2.convexityDefects(cnt, hidx)
        except cv2.error:
            return None
        if dfs is None:
            return None
        # OpenCV 4 는 (N,1,4), 5 는 (N,4) 를 돌려준다
        dfs = np.asarray(dfs).reshape(-1, 4)
        if len(dfs) < 2:
            return None
        dfs = dfs[np.argsort(-dfs[:, 3])][:2]
        need = max(3.0, (float(p["split_depth_mm"]) / mm) if mm > 0 else 3.0)
        if dfs[1, 3] / 256.0 < need:
            return None
        c2 = cnt.reshape(-1, 2)
        f1 = tuple(int(v) for v in c2[int(dfs[0, 2])])
        f2 = tuple(int(v) for v in c2[int(dfs[1, 2])])
        cut = g["mask"].copy()
        cv2.line(cut, f1, f2, 0, 2)
        nn, ll, st, _s = cv2.connectedComponentsWithStats((cut > 0).astype(np.uint8), 8)
        pieces = [j for j in range(1, nn) if st[j, cv2.CC_STAT_AREA] * mm * mm >= min_a]
        if len(pieces) != 2:
            return None
        out = []
        sid = int(round(g["cx"] * 1000 + g["cy"]))     # 갈라진 짝을 묶는 표식
        for j in pieces:
            gp = _mk((ll == j).astype(np.uint8) * 255)
            if gp is None or gp["sol"] < split_sol:
                return None
            gp["split"] = True
            gp["edge"] = gp["edge_frac"] > 0
            gp["rescued"] = g["rescued"]
            gp["sid"] = sid
            out.append(gp)
        return out

    final_items = []
    for g in items:
        if not g["merged"]:
            two = _split(g)
            if two:
                final_items.extend(two)
                continue
        final_items.append(g)

    # ---- 3단계: 덩어리마다 필터를 적용하고 좌표/모양을 낸다 ----------------
    final = np.zeros((H, W), np.uint8)
    cands = []
    shape_fill = float(p["shape_fill"])

    def _emit(g):
        """덩어리 하나를 결과 dict 로 만든다 (본 경로와 색거리 보조 경로 공용)."""
        # 함정 9: 면적 임계는 픽셀이 아니라 mm^2. 카메라 거리가 바뀌어도 그대로 쓴다.
        if not (min_a <= g["area"] <= max_a):
            return None
        pts = g["pts"]
        hull = cv2.convexHull(pts)
        px, py = g["cx"], g["cy"]          # 무게중심 = 픽셀 이하 정밀도
        v = M @ np.array([px - cx0, py - cy0])
        (_c, (rw, rh), rang) = cv2.minAreaRect(pts)
        # 모양은 approxPolyDP 꼭짓점 수로 정하지 않는다. 20x44px 샘플은 모서리가
        # 한두 픽셀만 둥글어도 오각형이 되고, 허용오차를 키우면 홀쭉한 직사각형이
        # 삼각형이 된다 (실제로 그랬다). 대신 '볼록껍질이 최소 삼각형/최소 사각형을
        # 얼마나 채우는가' 로 정한다. 함정 6 대로 모양으로 거르지는 않고 이름표만.
        ha = cv2.contourArea(hull)
        tri_a = cv2.minEnclosingTriangle(hull)[0]
        rect_a = max(1e-6, rw * rh)
        tri_fill = (ha / tri_a) if tri_a > 0 else 0.0
        rect_fill = ha / rect_a
        peri = cv2.arcLength(hull, True)
        approx = cv2.approxPolyDP(hull, 0.02 * peri, True).reshape(-1, 2)
        nv = len(approx)
        if tri_fill >= shape_fill and tri_fill > rect_fill:
            shape = "triangle"
        elif rect_fill >= shape_fill or nv == 4:
            shape = "quad"
        else:
            shape = "polygon%d" % nv
        c = {
            "shape": shape,
            "x_mm": round(float(v[0] * mm), 2),
            "y_mm": round(float(-v[1] * mm), 2),       # 이미지 Y는 아래로 증가 -> 부호 반전
            "x_px": round(px, 1), "y_px": round(py, 1),
            "w_mm": round(rw * mm, 2), "h_mm": round(rh * mm, 2),
            "angle_deg": round(float(rang), 1),
            "area_mm2": round(g["area"], 2),
            "solidity": round(g["sol"], 2),
            "mean_dev_pct": round(g["dev"], 1),
            "vertices_px": [[int(x), int(y)] for x, y in approx],
            "_poly": approx.reshape(-1, 1, 2).astype(np.int32),
            "_cnt": pts,
            "_sid": g.get("sid"),
        }
        for k in ("edge", "rescued", "split", "merged", "color_only"):
            if g.get(k):
                c[k] = True
        return c

    citems = []                      # (결과 dict, 덩어리) 짝. 엣지 보완이 쓴다
    for g in final_items:
        c = _emit(g)
        if c is None:
            continue
        cands.append(c)
        citems.append([c, g])
        final[g["mask"] > 0] = 255

    # ---- 3.5단계: 색거리(dE) 보조 경로 -----------------------------------
    # 기존 히스테리시스에 OR 로 섞으면 테두리 반사 얼룩까지 부풀어 이웃 칩과 한
    # 덩어리가 되면서 '있던 검출이 사라졌다' (실측 151630: 14 -> 12). 그래서
    # 완전히 분리된 경로로 두고, 이미 검출된 영역 위는 지운다. 이 경로는
    # 검출을 줄일 수 없다.
    # 철판 모드에서는 반드시 끈다. 그쪽 ROI 는 화면 전체(책상·케이스·철판이
    # 뒤섞임)라 '배경이 한 재질' 이라는 전제가 깨져 잡티가 쏟아졌다
    # (실측 161958 11 -> 24, 162101 12 -> 26).
    if float(p.get("de_k", 0) or 0) > 0 and wafer.surface == "wafer":
        de = _color_distance(bgr, roi, r_px)
        de_med = float(np.median(de[roib]))   # 샘플은 소수라 사실상 '표면 색잡음'
        de_t = max(float(p["de_floor"]), float(p["de_k"]) * de_med)
        cm = ((de > de_t) & roib & notblown).astype(np.uint8) * 255
        cm = cv2.morphologyEx(cm, cv2.MORPH_OPEN, _disk(ko))
        cm = cv2.morphologyEx(cm, cv2.MORPH_CLOSE, _disk(kc))
        cm = _fill_holes(cm)
        cm = cv2.bitwise_and(cm, cv2.bitwise_not(final))
        nn, ll, st, _s = cv2.connectedComponentsWithStats((cm > 0).astype(np.uint8), 8)
        for i in range(1, nn):
            g = _mk((ll == i).astype(np.uint8) * 255)
            if g is None:
                continue
            # 본 경로보다 엄하게 본다 (얼룩 배제): 볼록도는 split_solidity 요구.
            if (g["edge_frac"] > edge_max or g["sol"] < split_sol
                    or not (min_a <= g["area"] <= max_a)):
                continue
            near = 0.5 * math.sqrt(g["area"]) / mm if mm > 0 else 0.0
            if any(math.hypot(g["cx"] - c["x_px"], g["cy"] - c["y_px"]) < near
                   for c in cands):
                continue                      # 이미 잡은 샘플과 같은 것
            g["color_only"] = True
            c = _emit(g)
            if c is None:
                continue
            cands.append(c)
            citems.append([c, g])
            final[g["mask"] > 0] = 255

    # ---- 3.7단계: 엣지 보완 -----------------------------------------------
    # 색 마스크가 조각의 일부만 잡은 샘플을, Canny 엣지의 닫힌 다각형으로
    # 갈아끼운다. 확정된 샘플의 윤곽만 고치고 새 샘플은 만들지 않는다.
    edge_done = []
    if bool(p.get("edge_complete", True)) and citems:
        gb = cv2.GaussianBlur(gray, (3, 3), 0)
        cover_min = float(p["edge_cover_min"])
        grow_max = float(p["edge_grow_max"])
        sol_min = float(p["edge_solidity_min"])
        vmax = int(p["edge_vertices_max"])
        other_max = float(p["edge_other_overlap_max"])
        k3 = np.ones((3, 3), np.uint8)
        masks = [(it[1]["mask"] > 0) for it in citems]
        pixs = [int(m.sum()) for m in masks]
        pending = [i for i in range(len(citems))]
        for lo, hi in p.get("edge_canny", [[20, 60], [12, 40]]):
            if not pending:
                break
            e = cv2.Canny(gb, int(lo), int(hi))
            e = cv2.bitwise_and(e, roi)          # ROI 안만 본다
            e = cv2.morphologyEx(e, cv2.MORPH_CLOSE, k3)   # 한두 픽셀 끊김을 잇는다
            cs, _hh = cv2.findContours(e, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
            polys = []
            for cnt in cs:
                if len(cnt) < 3:
                    continue
                hull = cv2.convexHull(cnt)
                ha = cv2.contourArea(hull)
                if ha <= 0 or not (min_a <= ha * mm * mm <= max_a):
                    continue
                if cv2.contourArea(cnt) / ha < sol_min:
                    continue
                peri = cv2.arcLength(cnt, True)
                nv = len(cv2.approxPolyDP(cnt, 0.05 * peri, True))
                if not (3 <= nv <= vmax):
                    continue
                # 마스크는 그 다각형의 바운딩 박스 크기로만 만든다. 전체 화면
                # 크기로 만들면 컨투어 수백 개 x 1280x720 이라 메모리가 터진다.
                x, y, bw, bh = cv2.boundingRect(hull)
                pm = np.zeros((bh, bw), np.uint8)
                cv2.fillConvexPoly(pm, np.int32(hull) - [x, y], 255)
                polys.append((ha, hull, (x, y, bw, bh), pm > 0))
            polys.sort(key=lambda t: -t[0])
            still = []
            for i in pending:
                best = None
                for ha, hull, (x, y, bw, bh), pb in polys:
                    sl = (slice(y, y + bh), slice(x, x + bw))
                    if int((pb & masks[i][sl]).sum()) < cover_min * pixs[i]:
                        continue                 # 이 조각을 충분히 덮지 못한다
                    if ha > grow_max * pixs[i]:
                        continue                 # 너무 커졌다 = 배경까지 삼켰다
                    if any(j != i and int((pb & masks[j][sl]).sum()) > other_max * pixs[j]
                           for j in range(len(citems))):
                        continue                 # 옆 샘플을 물었다
                    if best is None or ha > best[0]:
                        best = (ha, hull)
                if best is None:
                    still.append(i)
                    continue
                nm = np.zeros((H, W), np.uint8)
                cv2.fillConvexPoly(nm, np.int32(best[1]), 255)
                g2 = _mk(nm)
                if g2 is None:
                    still.append(i)
                    continue
                for k in ("edge", "rescued", "split", "merged", "color_only"):
                    g2[k] = citems[i][1].get(k, False)
                c2 = _emit(g2)                   # 중심/모양/치수 계산은 그대로 재사용
                if c2 is None:
                    still.append(i)
                    continue
                c2["edge_completed"] = True
                cands[cands.index(citems[i][0])] = c2
                citems[i] = [c2, g2]
                masks[i] = g2["mask"] > 0
                pixs[i] = g2["pix"]
                final[g2["mask"] > 0] = 255
                edge_done.append(c2)
            pending = still

    # 번호: 왼쪽 위 -> 오른쪽 아래 읽는 순서 (행으로 묶고 그 안에서 x 순)
    row_h = max(1.0, 0.16 * r_px)
    cands.sort(key=lambda c: (round(c["y_px"] / row_h), c["x_px"]))
    for idx, c in enumerate(cands, 1):
        c["no"] = idx

    res.samples = cands
    res.mask = final
    if edge_done:
        res.info.append("edge-completed: "
                        + " ".join("#%d" % c["no"] for c in sorted(
                            edge_done, key=lambda c: c["no"])))

    # 플래그 경고는 번호를 매긴 뒤에 (사람이 annotated 에서 찾아볼 수 있게)
    for c in cands:
        if c.get("edge"):
            res.warnings.append("sample #%d touches the sensing edge; "
                                "position may be off" % c["no"])
        if c.get("rescued"):
            res.warnings.append("sample #%d separated from edge glare; "
                                "check outline" % c["no"])
        if c.get("color_only"):
            res.warnings.append("sample #%d found by color only; "
                                "check outline" % c["no"])
    pairs = {}
    for c in cands:
        if c.get("split") and c.get("_sid") is not None:
            pairs.setdefault(c["_sid"], []).append(c["no"])
    for nos in pairs.values():
        res.warnings.append("sample #%s split from a touching pair; check outline"
                            % ",#".join(str(x) for x in sorted(nos)))

    # ---- 경고 ------------------------------------------------------------
    if plate:
        return done()          # 아래는 전부 '원판 윤곽' 진단이라 철판 모드엔 없다
    if wafer.clipped_by_frame:
        res.warnings.append("wafer is cut off by the frame")
    if wafer.tilt_deg > 15:
        res.warnings.append("camera tilted %.0f deg" % wafer.tilt_deg)
    fill = wafer.major_px / float(min(H, W))
    if fill < 0.55:
        res.warnings.append("wafer fills only %.0f%% of frame" % (fill * 100))
    if wafer.roi_fit < 0.70:
        res.warnings.append("sensing area is mostly not the wafer (%.0f%% match)"
                            % (wafer.roi_fit * 100))
    # 윤곽이 못 미더우면 mm_per_px 가 통째로 틀어진다. 좌표를 믿으면 안 된다.
    if wafer.ray_coverage < 0.75:
        res.warnings.append("wafer outline unreliable: only %.0f%% of rays usable"
                            % (wafer.ray_coverage * 100))
    if not (0.85 <= wafer.ellipse_over_silhouette <= 1.20):
        res.warnings.append("wafer outline unreliable: ellipse/silhouette area = %.2f"
                            % wafer.ellipse_over_silhouette)
    if wafer.silhouette_solidity < 0.95:
        # 원판은 플랫이 있어도 볼록하다. 오목하면 마스크가 깨진 것 (반사광이
        # 잘려나갔거나 배경이 들러붙었거나). 이때 스케일을 믿으면 안 된다.
        res.warnings.append("wafer outline unreliable: silhouette not convex (%.2f)"
                            % wafer.silhouette_solidity)
    # 포화 픽셀은 후보에서 아예 빼므로 그 영역은 '보이지 않는 영역' 이다.
    # 45% 는 너무 늦다 - 26.4% 인 사진(115011)에서 경고 없이 칩 하나를 잃었다.
    if wafer.blown_pct > 15:
        res.warnings.append("glare covers %.0f%% of the sensing area; samples "
                            "inside it will be missed - diffuse the light"
                            % wafer.blown_pct)
    return done()


def samples_public(samples):
    """내부 필드(_cnt, _poly 등 "_" 로 시작하는 것)를 뺀 JSON/CSV 용 리스트."""
    out = []
    for s in samples:
        d = {k: v for k, v in s.items() if not k.startswith("_")}
        no = d.pop("no", 0)
        out.append({"no": no, **d})
    return out


# --------------------------------------------------------------------------
# annotated.png
# --------------------------------------------------------------------------
FONT = cv2.FONT_HERSHEY_SIMPLEX
# 함정 10: cv2.putText 는 한글을 렌더링 못해 네모로 깨진다. 이미지 텍스트는 전부 영문.


def _overlap(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _label_box(img, text, org, fs, th, bg, fg, taken=None, anchor=None):
    """작은 박스 안에 라벨을 그린다.

    화면 밖으로 나가면 반대쪽으로 접고, taken 이 주어지면 이미 놓인 라벨과
    겹치지 않는 자리를 찾는다 (샘플이 10개를 넘으면 라벨끼리 겹쳐서 정작
    확인하려는 칩을 가려 버린다).
    """
    (tw, tht), base = cv2.getTextSize(text, FONT, fs, th)
    h, w = img.shape[:2]
    pad = 3
    bw, bh = tw + 2 * pad, tht + 2 * pad + base

    def place(x, y):
        x = min(max(1, x), w - bw - 1)
        y = min(max(bh + 1, y), h - 2)
        return x, y

    cands = [org]
    if taken is not None and anchor is not None:
        ax, ay = anchor
        for dx, dy in ((8, -8), (8, 20), (-bw - 8, -8), (-bw - 8, 20),
                       (-bw // 2, -18), (-bw // 2, 30), (8, -30), (8, 42),
                       (-bw - 8, -30), (-bw - 8, 42)):
            cands.append((ax + dx, ay + dy))
    x, y = place(int(cands[0][0]), int(cands[0][1]))
    if taken is not None:
        for cx, cy in cands:
            x2, y2 = place(int(cx), int(cy))
            r = (x2, y2 - bh, x2 + bw, y2)
            if not any(_overlap(r, t) for t in taken):
                x, y = x2, y2
                break
    if taken is not None:
        taken.append((x, y - bh, x + bw, y))
    cv2.rectangle(img, (x, y - tht - 2 * pad), (x + bw, y + base), bg, -1)
    cv2.putText(img, text, (x + pad, y - pad), FONT, fs, fg, th, cv2.LINE_AA)


def _sample_color(s):
    """테두리 = 주황, 색거리 보조 경로 = 하늘색, 그 외 = 초록."""
    if s.get("edge"):
        return (0, 165, 255)
    if s.get("color_only"):
        return (255, 191, 0)
    return (0, 255, 0)


def draw_info_panel(img, info_lines, fs=None, th=None):
    """좌측 상단 정보 패널 - 크기를 getTextSize 로 실제 재서 맞춘다.

    상수로 박으면 해상도가 바뀔 때 반드시 삐져나온다. 잘라낸 영역이 아니라
    전체 프레임에 그리고 싶을 때가 있어(calib) 함수로 뺐다.
    """
    H, W = img.shape[:2]
    if fs is None:
        fs = max(0.4, min(W, H) / 1400.0)
    if th is None:
        th = max(1, int(round(fs * 2)))
    lines = [t for t in info_lines if t]
    if not lines:
        return img
    sizes = [cv2.getTextSize(t, FONT, fs, th)[0] for t in lines]
    pw = min(max(s[0] for s in sizes) + 16, W - 12)
    lh = max(s[1] for s in sizes) + 8
    ph = lh * len(lines) + 10
    ov = img.copy()
    cv2.rectangle(ov, (6, 6), (6 + pw, 6 + ph), (0, 0, 0), -1)
    cv2.addWeighted(ov, 0.55, img, 0.45, 0, img)
    for i, t in enumerate(lines):
        cv2.putText(img, t, (14, 6 + 8 + lh * (i + 1) - 6), FONT, fs,
                    (255, 255, 255), th, cv2.LINE_AA)
    return img


def annotate(bgr, res, info_lines=()):
    img = bgr.copy()
    H, W = img.shape[:2]
    fs = max(0.4, min(W, H) / 1400.0)
    th = max(1, int(round(fs * 2)))
    w = res.wafer

    if res.roi is not None and res.roi.any():
        cs, _ = cv2.findContours(res.roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(img, cs, -1, (255, 200, 0), 1)   # ROI 경계는 얇게

    for q in (res.marker_polys or []):
        cv2.polylines(img, [np.int32(q).reshape(-1, 1, 2)], True, (255, 128, 0), 2)
    for mk in (res.markers or []):
        _label_box(img, "id%d" % mk["id"],
                   (int(mk["center_px"][0]) + 6, int(mk["center_px"][1]) - 6),
                   fs, th, (255, 128, 0), (255, 255, 255))

    if w.surface == "plate":
        # 원판이 없으므로 타원/플랫 대신 ROI 사각형(위에서 이미 그렸다)과
        # 좌표 원점인 화면 중심 십자만 그린다.
        cv2.drawMarker(img, (W // 2, H // 2), (0, 255, 255), cv2.MARKER_CROSS, 28, 2)
    if w.found:
        c = (int(round(w.center_px[0])), int(round(w.center_px[1])))
        cv2.ellipse(img, c, (int(w.major_px / 2), int(w.minor_px / 2)),
                    w.theta_deg, 0, 360, (0, 255, 255), 2)
        cv2.drawMarker(img, c, (0, 255, 255), cv2.MARKER_CROSS, 24, 2)
        # 플랫은 '중심에서 뻗는 방향' 이 아니라 찾아낸 직선 변 그 자체를 그린다
        if w.flat_line_px is not None:
            (fx1, fy1), (fx2, fy2) = w.flat_line_px
            cv2.line(img, (int(round(fx1)), int(round(fy1))),
                     (int(round(fx2)), int(round(fy2))), (255, 0, 255), 3)
            mid = (int(round((fx1 + fx2) / 2)), int(round((fy1 + fy2) / 2)))
            _label_box(img, "FLAT", mid, fs, th, (255, 0, 255), (255, 255, 255))

    # 샘플 박스를 먼저 다 그리고, 라벨은 나중에 서로 안 겹치게 놓는다.
    taken = []
    if info_lines:
        taken.append((0, 0, 420, 200))     # 좌상단 정보 패널 자리는 미리 막아 둔다
    for s in res.samples:
        # minAreaRect 박스로 그리면 삼각형 샘플이 화면에서 사각형으로 보인다.
        # 실제 꼭짓점(vertices_px) 다각형을 그린다. 테두리 샘플은 주황색.
        poly = s.get("_poly")
        if poly is None:
            poly = np.int32(cv2.boxPoints(cv2.minAreaRect(s["_cnt"])))
        col = _sample_color(s)
        cv2.polylines(img, [np.int32(poly).reshape(-1, 1, 2)], True, col, 2)
        q = np.int32(poly).reshape(-1, 2)
        taken.append((q[:, 0].min(), q[:, 1].min(), q[:, 0].max(), q[:, 1].max()))
    for s in res.samples:
        p = (int(round(s["x_px"])), int(round(s["y_px"])))
        sh = s.get("shape", "quad")
        tag = "T" if sh == "triangle" else ("P" if sh.startswith("polygon") else "")
        if s.get("edge_completed"):
            tag += "E"                 # 윤곽을 Canny 다각형으로 갈아끼운 샘플
        col = _sample_color(s)
        cv2.drawMarker(img, p, (0, 0, 255), cv2.MARKER_CROSS, 12, 1)
        _label_box(img, "%d%s %+.1f/%+.1f" % (s["no"], tag, s["x_mm"], s["y_mm"]),
                   (p[0] + 8, p[1] - 8), fs, th, (0, 0, 0), col,
                   taken=taken, anchor=p)

    draw_info_panel(img, info_lines, fs, th)

    # 우측 하단 10mm 눈금자
    if w.mm_per_px > 0:          # 미보정 철판 모드(0)는 눈금자를 그리지 않는다
        L = int(round(10.0 / w.mm_per_px))
        if 5 < L < W - 40:
            y = H - 30
            x2 = W - 20
            x1 = x2 - L
            cv2.line(img, (x1, y), (x2, y), (255, 255, 255), 3)
            cv2.line(img, (x1, y - 6), (x1, y + 6), (255, 255, 255), 3)
            cv2.line(img, (x2, y - 6), (x2, y + 6), (255, 255, 255), 3)
            _label_box(img, "10 mm", (x1, y - 10), fs, th, (0, 0, 0), (255, 255, 255))
    return img
