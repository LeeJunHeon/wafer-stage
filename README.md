# wafer_stage

카메라로 웨이퍼 위 샘플 위치를 검출하고, 그 좌표를 갠트리 스테이지의 기계좌표로
변환하는 프로그램.

## 폴더 구조

```
자동 측정 프로그램/
├── wafer_stage/            # 이 저장소 (코드만)
│   ├── main.py             # 진입점. GUI 촬영 / 저장된 사진 오프라인 검출
│   ├── camera.py           # 카메라 열기, 초점, 단발 촬영
│   ├── detect.py           # 원판/샘플 검출 엔진 (GUI 의존성 없음)
│   ├── report.py           # result.json / samples.csv / annotated.png 저장
│   ├── calib.py            # 픽셀 <-> 기계좌표 보정 도구 (CLI)
│   ├── paths.py            # 경로 정의 (코드와 데이터 분리)
│   ├── settings.json       # 촬영/검출 파라미터
│   ├── firmware/stage_v6/  # 아두이노 스케치 (.ino)
│   └── assets/             # aruco_markers_30mm.pdf (실제 크기 100% 로 인쇄)
└── data/                   # 저장소 밖. 코드와 무관한 산출물
    ├── out/                # 촬영 결과 폴더 + log.jsonl
    └── calib_matrix.json   # calib.py fit 이 만든 변환행렬
```

`data` 위치는 `settings.json` 의 `data_dir` (기본 `../data`, wafer_stage 기준 상대경로).

## 실행 (wafer_stage 폴더 안에서)

```
python main.py                                   # GUI 촬영
python main.py --image ..\data\out\<폴더>\raw.png  # 저장된 사진으로 검출만
python calib.py fit                              # 마커로 보정 -> data\calib_matrix.json
python calib.py samples                          # 검출 결과를 기계좌표로 출력
```

`python calib.py px2mm 700 500` / `mm2px 100 50` 로 단건 변환도 된다.

## 아두이노

스케치는 `firmware/stage_v6/` 에 둔다.
