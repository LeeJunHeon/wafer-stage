//====================================================================
// 4축 스텝모터 제어 V10 — 절대좌표 + 원점 + 소프트리밋 + 위치 기억(세션 단위)
//                        + 릴레이 10 + 호밍 센서 입력 8
//
// V9 에서 바뀐 것:
//   배선이 전부 바뀌었다(아래 새 핀맵). 핀은 '드라이버 번호' 로 정의하고,
//             축(X1·X2·Y·Z) ↔ 드라이버 배정은 맨 위 배정 블록 4줄로 정한다.
//   펄스 출력을 일반화했다. V9 는 PORTE/PORTG 와 비트마스크를 하드코딩했다.
//             V10 은 setup 에서 각 축 PUL 핀의 포트 레지스터·비트를 구해 두고,
//             한 펄스에 움직일 축들을 포트별로 묶어 포트당 한 번씩 쓴다
//             (같은 포트면 한 번의 쓰기로 동시 출력). 배정을 바꿔도 코드는 그대로.
//             펄스 폭·DIR 준비 시간·속도/가속 프로파일은 V9 와 같다.
//   릴레이 10개(r / rs). 호밍 센서 8개 읽기(hs / hw) — 이동·원점에는 아직 안 쓴다.
//   배선이 바뀌었으므로 EEPROM 위치 기록을 새로 잡는다 → EE_MAGIC "POSA".
//             V9 이하로 저장한 기록은 '기록 없음' 으로 읽힌다(형식 자체는 V9 와 같다).
// 그 밖의 명령·출력 형식(st 한 줄 포함)은 V9 와 같다.
//
// [Z 규약]  Z=0 이 맨 위(들어 올린 자리)다. 값이 커질수록 아래로 내려간다.
//           펄스 부호도 같다(+ = 아래). 원점은 위쪽 하드스톱으로 밀어서 잡고,
//           거기서 HOME_GAP 만큼 아래로 물러난 자리가 0 이다(X·Y 와 같은 규칙).
//
// Arduino Mega 2560 + MotorBank MSD-224 x4
//
//   드라이버 (아두이노는 ENA+/DIR+/PUL+ 만 제어, − 쪽은 GND 공통. ENA LOW = 동작)
//     드라이버1 = X1 : ENA=D39  DIR=D41  PUL=D5 (PE3)
//     드라이버2 = X2 : ENA=D43  DIR=D45  PUL=D4 (PG5)
//     드라이버3 = Y  : ENA=D47  DIR=D49  PUL=D3 (PE5)
//     드라이버4 = Z  : ENA=D51  DIR=D53  PUL=D2 (PE4)
//   릴레이 (출력, 전원·리셋 시 전부 꺼짐)
//     R1 D22(노) R2 D24(주) R3 D26(보) R4 D28(갈) R5 D30(파)
//     R6 D32(녹) R7 D34(노) R8 D36(주) R9 D38(빨) R10 D40(검)
//   호밍 센서 (입력 풀업. 평소 HIGH, 닿으면 GND 와 접촉 → LOW = 닿음)
//     H1 D23(검) H2 D25(빨) H3 D27(주) H4 D29(노)
//     H5 D31(녹) H6 D33(파) H7 D35(갈) H8 D37(보)
//
//   160 펄스 = 1mm
//   X 스트로크 39,620 펄스 (247.6mm)   Y 스트로크 39,640 펄스 (247.8mm)
//   Z 스트로크 7,200 펄스 (45mm) — 실측값 (Z_PPMM 도 실측 확인)
//   위 세 값은 기계 한계다. 앱(settings.json "limits")은 이 안에서 실사용 범위를
//   따로 정한다 — 앱 설정이 여기보다 크면 앱이 연결할 때 경고를 남긴다.
//
// 시리얼: 115200 bps, 줄 끝 = "새 줄". 명령어와 숫자 사이 띄어쓰기 필수.
//
// [핵심 규칙]  원점이 없으면 fz 와 jx/jy/jz 외에는 아무것도 움직이지 않는다.
//              축 배정이 잘못되면(아래 배정 블록) 모든 이동 명령을 거부한다.
//
// [위치 기억]  위치는 RAM. EEPROM에는 "저장 이후 움직였는가"만 표시.
//   첫 이동 시  ok=0 기록 (세션에 1회)   /   save 시  ok=1 기록
//   전원 켤 때  ok=1 이면 복원, ok=0 이면 원점없음 (save 안 하고 꺼진 것)
//   → 세션당 EEPROM 쓰기 2회. 수명 걱정 없음.
//   파이썬이 메인이면: 이동 보고를 파일에 기록하고, 파킹/종료 때 save 전송.
//
// [원점]
//   fz x [탐색펄스] / fz y / fz z   - 그만큼 0 쪽으로 밀고 2mm 이격 후 0
//                                     (기본 800 = 5mm, 상한 1600. Z 는 위쪽이 0 쪽)
//   z / zx / zy / zz  지금 위치를 0 으로 등록 (움직이지 않음. z 는 X·Y 만)
//   sp x y           PC가 알려준 위치로 세팅 + 원점OK  (파이썬 복구용)
//   home             (0,0) 으로 복귀 (X·Y 만)
//
// [절대 이동]  gx 16000   gy 8000   gz 1600   g 16000 8000   mx 100.0   my 50.5   mz 10.0
// [상대 이동]  x 1600   y -1600   x1 200   x2 200
// [수동 이동]  jx 1600   jy -1600   jz 800   (원점 없어도 됨. 8000 펄스까지. 범위 밖이면 그 축 원점 해제)
// [테스트]     rx 5000 10   ry 5000 10   fx 5000 5   fy 5000 5
// [상태]       p  (사람용)     st  (파싱용 한 줄: ST X= Y= X2= Z= HX= HY= HZ= DIRTY=)
// [릴레이]     r <n> 1 / r <n> 0 (n=1~10)   r all 0   rs (RS R1=0 ... R10=0, 1=켜짐)
// [호밍 센서]  hs (HS H1=0 ... H8=0, 1=닿음)   hw (감시 모드: 바뀔 때마다 출력, 아무 입력이나 오면 끝)
// [기타]       save   forget   v a b w   e 0/1   !
//====================================================================
#include <EEPROM.h>

// ==================================================================
//  배정 블록 — 축 ↔ 드라이버 번호(1~4), 릴레이 극성
//  확정 배정 (2026-10-26)
// ==================================================================
const uint8_t AXIS_X1_DRV = 1;   // PUL 5 (PE3)
const uint8_t AXIS_X2_DRV = 2;   // PUL 4 (PG5)
const uint8_t AXIS_Y_DRV  = 3;   // PUL 3 (PE5)
const uint8_t AXIS_Z_DRV  = 4;   // PUL 2 (PE4)

// true = 릴레이 입력이 LOW 일 때 켜짐(흔한 릴레이 모듈). HIGH 에서 켜지면 false.
const bool RELAY_ACTIVE_LOW = true;

// ==================================================================
//  핀 표
// ==================================================================
struct DrvPins { uint8_t ena, dir, pul; };
const DrvPins DRV[4] = {
  { 39, 41, 5 },   // 드라이버1
  { 43, 45, 4 },   // 드라이버2
  { 47, 49, 3 },   // 드라이버3
  { 51, 53, 2 },   // 드라이버4
};

const uint8_t N_RELAY = 10;
const uint8_t RELAY_PIN[N_RELAY] = { 22, 24, 26, 28, 30, 32, 34, 36, 38, 40 };

const uint8_t N_HOME = 8;
const uint8_t HOME_PIN[N_HOME] = { 23, 25, 27, 29, 31, 33, 35, 37 };
const uint8_t  HS_SAMPLES = 5;     // 연속으로 이만큼 같은 값이어야 상태 확정
const uint16_t HS_GAP_US  = 200;   // 표본 사이 간격

// 모터(물리) 번호. 배정 블록 순서와 같다.
enum Motor { MT_X1, MT_X2, MT_Y, MT_Z, N_MOTOR };
const uint8_t AXIS_DRV[N_MOTOR] = { AXIS_X1_DRV, AXIS_X2_DRV, AXIS_Y_DRV, AXIS_Z_DRV };

// setup 에서 배정을 검사하고 채운다. assignOk 가 false 면 아래 표는 쓰지 않는다.
bool              assignOk = false;
uint8_t           mDir[N_MOTOR];
volatile uint8_t *pulReg[N_MOTOR];   // PUL 핀의 출력 포트 레지스터
uint8_t           pulMask[N_MOTOR];  // 그 포트 안의 비트
// 한 펄스에 같이 나갈 PUL 비트를 포트별로 묶은 것(moveAxis)
struct PulGroup { volatile uint8_t *reg; uint8_t mask; };

const long  X_MAX    = 39620;
const long  Y_MAX    = 39640;
// 기계 한계(펄스). 앱의 가동범위 설정은 이 안에서 쓰는 실사용 범위다.
// 실측: 위 끝단에서 2mm 이격한 0 에서 아래 끝단까지 45mm (2026-09-16).
const long  Z_MAX    = 7200;     // 45mm — 실측값
const float Z_PPMM   = 160.0;    // 실측 확인 2026-09-14 (1mm 명령 = 1mm 이동)
const float PPMM     = 160.0;
const long  HOME_GAP = 320;

const bool X2_DIR_INVERT   = false;
// Z 가 반대로 움직이면(+ 인데 위로 간다) 이것만 true 로 바꾼다.
const bool Z_DIR_INVERT    = false;
const long BACKLASH_TAKEUP = 0;

const uint16_t PULSE_HIGH_US = 5;
const uint16_t DIR_SETUP_US  = 50;

float    vMax = 6000.0, accel = 40000.0, vStart = 300.0;
uint16_t pauseMs = 200;

long posX1 = 0, posX2 = 0, posY = 0, posZ = 0;
bool homedX = false, homedY = false, homedZ = false;
bool abortFlag = false;
bool eeDirty   = false;     // 저장 이후 이동이 있었는가 (RAM)

bool    relayOn[N_RELAY];   // 논리 상태(1 = 켜짐). 극성과 무관
uint8_t homeBits = 0;       // 확정된 호밍 상태. 비트 i = H(i+1) 닿음

enum Axis { AX_X, AX_Y, AX_Z, AX_X1, AX_X2 };

// ==================================================================
//  EEPROM — 슬롯 1개, 세션당 2회 쓰기
// ==================================================================
struct PosRec {
  uint32_t magic;
  int32_t  x1, x2, y, z;
  uint8_t  homedX, homedY, homedZ;
  uint8_t  ok;       // 1 = 저장 이후 이동 없음 (신뢰)  0 = 이동 있었음 (불신)
  uint8_t  sum;
};
// 기록 형식이 바뀌면 매직도 바꾼다 - 옛 기록을 새 구조로 읽으면 위치가 엉킨다.
// V10 은 형식은 같지만 배선이 전부 바뀌어 V9 위치를 믿을 수 없으므로 바꿨다.
const uint32_t EE_MAGIC = 0x504F5341UL;   // "POSA"
const int      EE_ADDR  = 0;

uint8_t recSum(const PosRec &r) {
  const uint8_t *p = (const uint8_t*)&r;
  uint8_t s = 0;
  for (size_t i = 0; i < sizeof(PosRec) - 1; i++) s += p[i];
  return (uint8_t)(0xFF - s);
}

void eeWrite(bool ok) {
  PosRec r;
  r.magic = EE_MAGIC;
  r.x1 = posX1; r.x2 = posX2; r.y = posY; r.z = posZ;
  r.homedX = homedX ? 1 : 0;
  r.homedY = homedY ? 1 : 0;
  r.homedZ = homedZ ? 1 : 0;
  r.ok  = ok ? 1 : 0;
  r.sum = recSum(r);
  EEPROM.put(EE_ADDR, r);     // 바뀐 바이트만 실제로 씀
  eeDirty = !ok;
}

int eeLoad() {   // 0=기록없음  1=복원  2=저장 안 하고 꺼짐
  PosRec r;
  EEPROM.get(EE_ADDR, r);
  if (r.magic != EE_MAGIC || recSum(r) != r.sum) return 0;
  if (!r.ok) return 2;
  posX1 = r.x1; posX2 = r.x2; posY = r.y; posZ = r.z;
  homedX = r.homedX; homedY = r.homedY; homedZ = r.homedZ;
  return 1;
}

void markDirty() { if (!eeDirty) eeWrite(false); }   // 세션 첫 이동에만 실제 쓰기

// ==================================================================
//  축 배정 검사 · 포트 캐시
// ==================================================================
const __FlashStringHelper* motorName(uint8_t m) {
  switch (m) {
    case MT_X1: return F("X1");
    case MT_X2: return F("X2");
    case MT_Y:  return F("Y");
    default:    return F("Z");
  }
}

// 같은 드라이버 두 번, 1~4 밖이면 오류를 찍고 false.
bool checkAssign() {
  bool ok = true;
  for (uint8_t m = 0; m < N_MOTOR; m++) {
    if (AXIS_DRV[m] < 1 || AXIS_DRV[m] > 4) {
      Serial.print(F("  [오류] 축 배정: ")); Serial.print(motorName(m));
      Serial.print(F("=드라이버")); Serial.print(AXIS_DRV[m]);
      Serial.println(F(" (1~4 밖)"));
      ok = false;
    }
    for (uint8_t k = 0; k < m; k++) {
      if (AXIS_DRV[k] == AXIS_DRV[m]) {
        Serial.print(F("  [오류] 축 배정: ")); Serial.print(motorName(k));
        Serial.print(F(" 와 ")); Serial.print(motorName(m));
        Serial.print(F(" 가 같은 드라이버")); Serial.println(AXIS_DRV[m]);
        ok = false;
      }
    }
  }
  if (!ok) Serial.println(F("  [오류] 배정 블록(AXIS_*_DRV)을 고쳐 다시 업로드할 때까지 모든 이동을 거부합니다"));
  return ok;
}

void cacheMotorPins() {
  for (uint8_t m = 0; m < N_MOTOR; m++) {
    const DrvPins &d = DRV[AXIS_DRV[m] - 1];
    mDir[m]    = d.dir;
    pulReg[m]  = portOutputRegister(digitalPinToPort(d.pul));
    pulMask[m] = digitalPinToBitMask(d.pul);
  }
}

// 부팅 로그용: "X1=D5(PE 0x08)"
void printMotorPul(uint8_t m) {
  const DrvPins &d = DRV[AXIS_DRV[m] - 1];
  Serial.print(motorName(m)); Serial.print(F("=D")); Serial.print(d.pul);
  Serial.print(F("(P")); Serial.print((char)('A' + digitalPinToPort(d.pul) - 1));
  Serial.print(F(" 0x"));
  if (pulMask[m] < 0x10) Serial.print('0');
  Serial.print(pulMask[m], HEX); Serial.print(')');
}

bool rejectIfBadAssign() {
  if (assignOk) return false;
  Serial.println(F("  [거부] 축 배정 오류 → 배정 블록(AXIS_*_DRV)을 고쳐 다시 업로드"));
  return true;
}

// ==================================================================
//  릴레이
// ==================================================================
// 정책: 전원 투입·리셋 시 전부 꺼짐, 명령으로만 켜진다.
//       비상정지(!)·forget·e 는 릴레이를 건드리지 않는다 — 측정 장비 쿨러 등의
//       전원을 모터 사정으로 함부로 끊지 않는다. 끄려면 r <n> 0 / r all 0.
uint8_t relayLevel(bool on) { return (on != RELAY_ACTIVE_LOW) ? HIGH : LOW; }

void relaySet(uint8_t i, bool on) {
  relayOn[i] = on;
  digitalWrite(RELAY_PIN[i], relayLevel(on));
}

void relaysInit() {
  // 순서가 중요하다: 꺼짐 레벨을 먼저 digitalWrite 한 뒤 pinMode(OUTPUT).
  // 반대로 하면 OUTPUT 으로 바뀌는 순간 핀이 LOW 로 나가 (LOW 에서 켜지는 릴레이가)
  // 잠깐 켜지는 펄스가 생긴다. 입력 상태에서 HIGH 를 쓰면 풀업이 켜지고,
  // OUTPUT 으로 바뀔 때 그 레벨 그대로 나간다.
  for (uint8_t i = 0; i < N_RELAY; i++) {
    relayOn[i] = false;
    digitalWrite(RELAY_PIN[i], relayLevel(false));
    pinMode(RELAY_PIN[i], OUTPUT);
  }
}

void printRS() {
  Serial.print(F("RS"));
  for (uint8_t i = 0; i < N_RELAY; i++) {
    Serial.print(F(" R")); Serial.print(i + 1); Serial.print('=');
    Serial.print(relayOn[i] ? 1 : 0);
  }
  Serial.println();
}

bool isUInt(const String &s) {
  if (s.length() == 0 || s.length() > 3) return false;
  for (unsigned int i = 0; i < s.length(); i++) if (!isDigit(s.charAt(i))) return false;
  return true;
}

void relayCmd(const String &s1, const String &s2, int nTok) {
  if (nTok < 3 || (s2 != "0" && s2 != "1")) {
    Serial.println(F("  [거부] r <1~10> <1|0>  또는  r all 0"));
    return;
  }
  bool on = (s2 == "1");
  if (s1 == "all") {
    if (on) { Serial.println(F("  [거부] r all 은 0 만 (켜기는 하나씩)")); return; }
    for (uint8_t i = 0; i < N_RELAY; i++) relaySet(i, false);
    Serial.println(F("  릴레이 전부 꺼짐"));
    printRS();
    return;
  }
  long n = isUInt(s1) ? s1.toInt() : 0;
  if (n < 1 || n > N_RELAY) {
    Serial.println(F("  [거부] 릴레이 번호는 1~10"));
    return;
  }
  relaySet(n - 1, on);
  Serial.print(F("  릴레이 R")); Serial.print(n);
  Serial.println(on ? F(" 켜짐") : F(" 꺼짐"));
  printRS();
}

// ==================================================================
//  호밍 센서 (읽기만. 이동·원점 로직에는 아직 연결하지 않는다)
// ==================================================================
uint8_t homeRaw() {
  uint8_t b = 0;
  for (uint8_t i = 0; i < N_HOME; i++)
    if (digitalRead(HOME_PIN[i]) == LOW) b |= (1 << i);   // LOW = 닿음
  return b;
}

// HS_SAMPLES 번 연속 같은 값이 나온 센서만 상태를 갱신한다(디바운스·노이즈).
// 흔들린 센서는 이전 확정값을 유지한다.
void homeSample() {
  uint8_t first = homeRaw();
  uint8_t same  = 0xFF;
  for (uint8_t k = 1; k < HS_SAMPLES; k++) {
    delayMicroseconds(HS_GAP_US);
    same &= (uint8_t)~(homeRaw() ^ first);
  }
  homeBits = (homeBits & ~same) | (first & same);
}

void homesInit() {
  for (uint8_t i = 0; i < N_HOME; i++) pinMode(HOME_PIN[i], INPUT_PULLUP);
  delay(2);                                 // 풀업이 자리 잡을 시간
  homeBits = homeRaw();
  for (uint8_t k = 0; k < 4; k++) homeSample();
}

void printHS() {
  Serial.print(F("HS"));
  for (uint8_t i = 0; i < N_HOME; i++) {
    Serial.print(F(" H")); Serial.print(i + 1); Serial.print('=');
    Serial.print((homeBits >> i) & 1);
  }
  Serial.println();
}

// 센서 ↔ 위치 확인용. 모터는 움직이지 않는다. 아무 입력이나 오면 끝.
void homeWatch() {
  Serial.println(F("--- 호밍 감시 (아무 입력이나 보내면 종료) ---"));
  homeSample();
  printHS();
  uint8_t prev = homeBits;
  while (!Serial.available()) {
    homeSample();
    if (homeBits != prev) {
      uint8_t ch = homeBits ^ prev;
      printHS();
      Serial.print(F("  바뀜"));
      for (uint8_t i = 0; i < N_HOME; i++) {
        if (ch & (1 << i)) {
          Serial.print(F(" H")); Serial.print(i + 1); Serial.print('=');
          Serial.print((homeBits >> i) & 1);
        }
      }
      Serial.println();
      prev = homeBits;
    }
    delay(5);
  }
  delay(20);                                // 줄 끝까지 다 들어오게
  while (Serial.available()) Serial.read(); // 종료시킨 입력은 명령으로 쓰지 않는다
  Serial.println(F("--- 감시 종료 ---"));
}

// ==================================================================
long  axPos(Axis ax)   { return (ax == AX_Y) ? posY : (ax == AX_Z) ? posZ : posX1; }
long  axMax(Axis ax)   { return (ax == AX_Y) ? Y_MAX : (ax == AX_Z) ? Z_MAX : X_MAX; }

// V7 추가 상수
const long HOME_SEARCH_DEFAULT = 800;    // fz 인자 없을 때 5mm 만 민다
const long HOME_SEARCH_MAX     = 1600;   // fz 탐색 상한 10mm (넘으면 거부)
const long JOG_MAX_PULSE       = 8000;   // jx/jy 한 번에 50mm 까지
bool  axHomed(Axis ax) { return (ax == AX_Y) ? homedY : (ax == AX_Z) ? homedZ : homedX; }
void  clearHomed(Axis ax) { if (ax == AX_Y) homedY = false; else if (ax == AX_Z) homedZ = false; else homedX = false; }
long  labs2(long v)    { return v < 0 ? -v : v; }

const __FlashStringHelper* axName(Axis ax) {
  switch (ax) {
    case AX_X:  return F("X ");
    case AX_Z:  return F("Z ");
    case AX_X1: return F("X1");
    case AX_X2: return F("X2");
    default:    return F("Y ");
  }
}

// 드라이버 4개 모두. 배정과 무관하게 표에서 직접 쓴다(배정 오류여도 안전하게 풀고 잠근다).
void motorsEnable(bool on) {
  for (uint8_t d = 0; d < 4; d++) digitalWrite(DRV[d].ena, on ? LOW : HIGH);
}
void motorsOn() { motorsEnable(true); }

void printStatus() {
  Serial.print(F("  v=")); Serial.print(vMax, 0);
  Serial.print(F(" a=")); Serial.print(accel, 0);
  Serial.print(F(" b=")); Serial.print(vStart, 0);
  Serial.print(F(" w=")); Serial.print(pauseMs); Serial.println(F("ms"));

  Serial.print(F("  X=")); Serial.print(posX1);
  Serial.print(F(" (")); Serial.print(posX1 / PPMM, 2); Serial.print(F("mm)"));
  Serial.print(homedX ? F(" [원점OK]") : F(" [원점없음]"));
  Serial.print(F("   Y=")); Serial.print(posY);
  Serial.print(F(" (")); Serial.print(posY / PPMM, 2); Serial.print(F("mm)"));
  Serial.print(homedY ? F(" [원점OK]") : F(" [원점없음]"));
  Serial.println(eeDirty ? F("   (저장 안 됨)") : F("   (저장됨)"));

  Serial.print(F("  Z=")); Serial.print(posZ);
  Serial.print(F(" (")); Serial.print(posZ / Z_PPMM, 2); Serial.print(F("mm)"));
  Serial.print(homedZ ? F(" [원점OK]") : F(" [원점없음]"));
  Serial.println(F("   (0 = 맨 위, + = 아래)"));

  if (posX1 != posX2) {
    Serial.print(F("  [!] X 틀어짐  X1=")); Serial.print(posX1);
    Serial.print(F(" X2=")); Serial.println(posX2);
  }
  if (!homedX || !homedY || !homedZ)
    Serial.println(F("  → 원점이 없는 축은 움직이지 않습니다. fz x / fz y / fz z 또는 sp x y"));
}

// p 전용: 배정 블록(축→드라이버→핀), 릴레이, 호밍 상태
void printWiring() {
  Serial.println(assignOk ? F("  배정 (축 → 드라이버 → ENA/DIR/PUL)")
                          : F("  배정 [오류] — 이동 거부 중"));
  for (uint8_t m = 0; m < N_MOTOR; m++) {
    Serial.print(F("    ")); Serial.print(motorName(m));
    Serial.print(F(" → 드라이버")); Serial.print(AXIS_DRV[m]);
    if (AXIS_DRV[m] >= 1 && AXIS_DRV[m] <= 4) {
      const DrvPins &d = DRV[AXIS_DRV[m] - 1];
      Serial.print(F(" → D")); Serial.print(d.ena);
      Serial.print(F("/D"));   Serial.print(d.dir);
      Serial.print(F("/D"));   Serial.print(d.pul);
    }
    Serial.println();
  }
  Serial.print(F("  릴레이 (RELAY_ACTIVE_LOW=")); Serial.print(RELAY_ACTIVE_LOW ? 1 : 0);
  Serial.print(F(")  ")); printRS();
  homeSample();
  Serial.print(F("  호밍 센서  ")); printHS();
}

// 파싱용 한 줄
void printST() {
  Serial.print(F("ST X="));  Serial.print(posX1);
  Serial.print(F(" Y="));    Serial.print(posY);
  Serial.print(F(" X2="));   Serial.print(posX2);
  Serial.print(F(" Z="));    Serial.print(posZ);
  Serial.print(F(" HX="));   Serial.print(homedX ? 1 : 0);
  Serial.print(F(" HY="));   Serial.print(homedY ? 1 : 0);
  Serial.print(F(" HZ="));   Serial.print(homedZ ? 1 : 0);
  Serial.print(F(" DIRTY=")); Serial.println(eeDirty ? 1 : 0);
}

bool requireHomed(Axis ax) {
  if (axHomed(ax)) return true;
  if (ax == AX_Z) {
    Serial.println(F("  [거부] 원점 미확정 → fz z 또는 zz"));
    return false;
  }
  Serial.print(F("  [거부] 원점 미확정 → fz "));
  Serial.print(ax == AX_Y ? F("y") : F("x"));
  Serial.println(F(" 또는 sp x y"));
  return false;
}

bool checkLimit(Axis ax, long target) {
  long hi = axMax(ax);
  if (target < 0 || target > hi) {
    Serial.print(F("  [거부] 범위 밖  목표=")); Serial.print(target);
    Serial.print(F("  허용=0~")); Serial.println(hi);
    return false;
  }
  return true;
}

// ------------------------------------------------------------------
void setDir(Axis ax, bool pos) {
  bool d2 = X2_DIR_INVERT ? !pos : pos;
  switch (ax) {
    case AX_X:  digitalWrite(mDir[MT_X1], pos); digitalWrite(mDir[MT_X2], d2); break;
    case AX_X1: digitalWrite(mDir[MT_X1], pos); break;
    case AX_X2: digitalWrite(mDir[MT_X2], d2);  break;
    case AX_Y:  digitalWrite(mDir[MT_Y], pos);  break;
    case AX_Z:  digitalWrite(mDir[MT_Z], Z_DIR_INVERT ? !pos : pos); break;
  }
  delayMicroseconds(DIR_SETUP_US);
}

// ax 가 움직이는 모터들을 포트별로 묶는다. 같은 포트면 비트를 OR 해 한 묶음 —
// 한 번의 쓰기로 동시에 올라가고 내려간다(X1·X2 동기). 반환값 = 묶음 수.
uint8_t buildPulGroups(Axis ax, PulGroup g[]) {
  uint8_t ms[2], nm = 0;
  switch (ax) {
    case AX_X:  ms[nm++] = MT_X1; ms[nm++] = MT_X2; break;
    case AX_X1: ms[nm++] = MT_X1; break;
    case AX_X2: ms[nm++] = MT_X2; break;
    case AX_Y:  ms[nm++] = MT_Y;  break;
    case AX_Z:  ms[nm++] = MT_Z;  break;
  }
  uint8_t ng = 0;
  for (uint8_t i = 0; i < nm; i++) {
    uint8_t m = ms[i], j = 0;
    while (j < ng && g[j].reg != pulReg[m]) j++;
    if (j == ng) { g[ng].reg = pulReg[m]; g[ng].mask = 0; ng++; }
    g[j].mask |= pulMask[m];
  }
  return ng;
}

void addPos(Axis ax, long d) {
  switch (ax) {
    case AX_X:  posX1 += d; posX2 += d; break;
    case AX_X1: posX1 += d; break;
    case AX_X2: posX2 += d; break;
    case AX_Y:  posY  += d; break;
    case AX_Z:  posZ  += d; break;
  }
}

// ---- 실제 펄스 출력 (검사 없음. 상위에서 검사) ----
void moveAxis(Axis ax, unsigned long steps, bool pos) {
  if (steps == 0) return;
  if (rejectIfBadAssign()) return;     // 상위에서 이미 막지만, 표가 비어 있으면 절대 쓰지 않는다
  markDirty();

  PulGroup grp[2];
  uint8_t  nGrp = buildPulGroups(ax, grp);
  setDir(ax, pos);

  unsigned long nAcc = 0;
  if (vMax > vStart) nAcc = (unsigned long)((vMax*vMax - vStart*vStart) / (2.0*accel));
  if (nAcc * 2UL > steps) nAcc = steps / 2UL;
  unsigned long decStart = steps - nAcc;

  float v = vStart;
  unsigned long emitted = 0;
  bool lagged = false;
  unsigned long t0 = micros();
  unsigned long next = t0;

  for (unsigned long i = 0; i < steps; i++) {
    if ((i & 0x3F) == 0 && Serial.available() && Serial.peek() == '!') {
      Serial.read(); abortFlag = true; break;
    }
    while ((long)(micros() - next) < 0) { }

    // 포트 레지스터를 포인터로 읽고-고쳐-쓰므로 그 사이 인터럽트를 막는다.
    // 묶음이 둘(다른 포트)이어도 연속 쓰기라 같은 펄스 폭 안에 함께 나간다.
    // 확정 배정에서는 X1(PE3)·X2(PG5) 가 다른 포트라 X 이동이 늘 두 묶음이다:
    // 올림 두 번이 한 cli 구간, 내림 두 번이 다른 한 cli 구간 안에서 연속으로 일어나
    // 두 PUL 의 시차는 몇 클럭(1µs 미만)이고, 둘 다 HIGH 폭은 PULSE_HIGH_US 이상이다.
    // 인터럽트가 끼어들 수 없으므로 X1·X2 의 펄스 수와 주기는 항상 같다.
    uint8_t sreg = SREG; cli();
    for (uint8_t k = 0; k < nGrp; k++) *grp[k].reg |= grp[k].mask;
    SREG = sreg;
    delayMicroseconds(PULSE_HIGH_US);
    sreg = SREG; cli();
    for (uint8_t k = 0; k < nGrp; k++) *grp[k].reg &= (uint8_t)~grp[k].mask;
    SREG = sreg;
    emitted++;

    if (i < nAcc)           v += accel / v;
    else if (i >= decStart) v -= accel / v;
    if (v > vMax)   v = vMax;
    if (v < vStart) v = vStart;

    next += (unsigned long)(1000000.0 / v);
    if ((long)(micros() - next) > 0) { next = micros(); lagged = true; }
  }

  unsigned long dt = micros() - t0;
  addPos(ax, pos ? (long)emitted : -(long)emitted);

  Serial.print(F("  ")); Serial.print(axName(ax));
  Serial.print(pos ? F(" + ") : F(" - "));
  Serial.print(F("cmd=")); Serial.print(steps);
  Serial.print(F(" out=")); Serial.print(emitted);
  Serial.print(F(" t=")); Serial.print(dt / 1000.0, 2); Serial.print(F("ms"));
  Serial.print(F(" | X=")); Serial.print(posX1);
  Serial.print(F(" Y=")); Serial.print(posY);
  Serial.print(F(" Z=")); Serial.print(posZ);
  if (posX1 != posX2) Serial.print(F("  [!] X 틀어짐"));
  if (lagged)         Serial.print(F("  [!] CPU"));
  if (abortFlag)      Serial.print(F("  [!] 중단"));
  Serial.println();
}

void moveRel(Axis ax, long delta) {
  if (delta == 0) return;
  if (!requireHomed(ax)) return;
  if (!checkLimit(ax, axPos(ax) + delta)) return;
  moveAxis(ax, labs2(delta), delta > 0);
}

void moveSingle(Axis ax, long delta) {
  if (delta == 0) return;
  if (!requireHomed(AX_X)) return;
  long cur = (ax == AX_X1) ? posX1 : posX2;
  if (!checkLimit(AX_X, cur + delta)) return;
  Serial.println(F("  (단독 이동 - 갠트리가 틀어집니다)"));
  moveAxis(ax, labs2(delta), delta > 0);
}

void gotoAbs(Axis ax, long target) {
  if (!requireHomed(ax)) return;
  if (!checkLimit(ax, target)) return;
  long d = target - axPos(ax);
  if (d == 0) { Serial.println(F("  이미 그 위치")); return; }

  if (BACKLASH_TAKEUP > 0 && d < 0 && (target - BACKLASH_TAKEUP) >= 0) {
    moveAxis(ax, labs2(d) + BACKLASH_TAKEUP, false);
    delay(pauseMs);
    moveAxis(ax, BACKLASH_TAKEUP, true);
    return;
  }
  moveAxis(ax, labs2(d), d > 0);
}

// ---- 수동 이동 (원점이 없어도 움직인다) ----
// 사람이 보면서 캐리지를 끝단까지 몰고 가는 용도다. 가동범위 밖이라고 거부하지
// 않는다 - 거부하면 원점이 있는 축을 하드스톱까지 몰고 갈 방법이 없다. 대신 범위를
// 벗어나는 순간 그 축의 원점을 해제한다(끝에 닿아 탈조하면 좌표를 못 믿는다).
// 한 번에 갈 수 있는 거리는 제한해 '크게 잘못 보내는' 사고를 막는다.
void jogRel(Axis ax, long delta) {
  if (delta == 0) return;
  if (labs2(delta) > JOG_MAX_PULSE) {
    Serial.println(F("  [거부] jx/jy/jz 는 한 번에 8000 펄스까지"));
    return;
  }
  if (axHomed(ax)) {
    long t = axPos(ax) + delta;
    if (t < 0 || t > axMax(ax)) {
      clearHomed(ax);
      Serial.print(F("  [!] ")); Serial.print(axName(ax));
      Serial.println(F(" 가동범위 밖 → 원점 해제 (다시 등록 필요)"));
      eeWrite(true);
    }
  }
  motorsOn();                 // e 1 로 풀어 둔 뒤에도 바로 움직일 수 있게
  moveAxis(ax, labs2(delta), delta > 0);
}

// ---- 자동 원점 (원점 없이 움직이는 유일한 명령) ----
void findZero(Axis ax, long searchLen) {
  // 인자가 없으면 짧게만 민다. 스트로크 전체를 미는 기본값은 이미 끝에 닿아
  // 있을 때 수십 초를 갈아 먹는다. 긴 거리는 잘라 주는 대신 거부한다 -
  // 사람이 끝단 근처까지 몰고 온 뒤에 쓰는 명령이다.
  if (searchLen <= 0) searchLen = HOME_SEARCH_DEFAULT;
  if (searchLen > HOME_SEARCH_MAX) {
    Serial.println(F("  [거부] fz 탐색은 1600 펄스(10mm)까지"));
    return;
  }

  Serial.print(F("--- 원점 탐색 (탐색 ")); Serial.print(searchLen);
  Serial.println(F(" 펄스) --- 끝에 닿으면 드르륵 소리 (정상)"));

  motorsOn();
  float sv = vMax, sa = accel;
  bool  hx = homedX, hy = homedY, hz = homedZ;
  homedX = false; homedY = false; homedZ = false;
  vMax = 1200.0; accel = 20000.0;

  moveAxis(ax, searchLen, false);
  delay(500);
  if (!abortFlag) moveAxis(ax, HOME_GAP, true);

  vMax = sv; accel = sa;
  homedX = hx; homedY = hy; homedZ = hz;

  if (abortFlag) { Serial.println(F("--- 중단됨. 원점 설정 안 됨 ---")); return; }
  if      (ax == AX_X) { posX1 = 0; posX2 = 0; homedX = true; }
  else if (ax == AX_Z) { posZ = 0; homedZ = true; }
  else                 { posY = 0; homedY = true; }
  eeWrite(true);
  Serial.println(F("--- 원점 설정 완료 (저장됨) ---"));
  printStatus();
}

void reciprocate(Axis ax, long n, int cycles) {
  if (!requireHomed(ax)) return;
  if (!checkLimit(ax, axPos(ax) + n)) return;
  Serial.println(F("--- 왕복 ---"));
  for (int c = 1; c <= cycles; c++) {
    Serial.print(F("[cyc ")); Serial.print(c); Serial.print('/');
    Serial.print(cycles); Serial.println(F("]"));
    moveAxis(ax, n, true);  if (abortFlag) break;
    delay(pauseMs);
    moveAxis(ax, n, false); if (abortFlag) break;
    delay(pauseMs);
  }
  Serial.println(F("--- 종료 ---"));
  printStatus();
}

void repeatSame(Axis ax, long n, int cycles, bool pos) {
  if (!requireHomed(ax)) return;
  long tot = n * (long)cycles;
  if (!checkLimit(ax, axPos(ax) + (pos ? tot : -tot))) return;
  Serial.println(F("--- 단방향 ---"));
  for (int c = 1; c <= cycles; c++) {
    Serial.print(F("[cyc ")); Serial.print(c); Serial.print('/');
    Serial.print(cycles); Serial.println(F("]"));
    moveAxis(ax, n, pos); if (abortFlag) break;
    delay(pauseMs);
  }
  Serial.println(F("--- 종료 ---"));
  printStatus();
}

// 배정 오류일 때 거부할 명령(모터를 움직이는 것 전부)
bool isMotionCmd(const String &c) {
  static const char* const M[] = { "fz", "home", "gx", "gy", "gz", "mx", "my", "mz", "g",
                                   "jx", "jy", "jz", "x", "y", "x1", "x2",
                                   "rx", "ry", "fx", "fy" };
  for (uint8_t i = 0; i < sizeof(M) / sizeof(M[0]); i++) if (c == M[i]) return true;
  return false;
}

// ------------------------------------------------------------------
void setup() {
  relaysInit();                // 맨 먼저 — 부팅 순간에도 릴레이가 켜지지 않게
  homesInit();

  // 드라이버 4개 핀은 배정과 무관하게 표 전체를 LOW 로(V9 와 같음. ENA LOW = 동작)
  for (uint8_t d = 0; d < 4; d++) {
    const uint8_t pins[3] = { DRV[d].ena, DRV[d].dir, DRV[d].pul };
    for (uint8_t k = 0; k < 3; k++) { pinMode(pins[k], OUTPUT); digitalWrite(pins[k], LOW); }
  }

  Serial.begin(115200);
  Serial.setTimeout(30);
  Serial.println(F("=== 3-AXIS V10 ==="));
  Serial.print(F("  X 0~")); Serial.print(X_MAX);
  Serial.print(F("  Y 0~")); Serial.print(Y_MAX);
  Serial.print(F("  Z 0~")); Serial.print(Z_MAX);
  Serial.println(F("   (160 pulse = 1mm, Z: 0 = 맨 위)"));

  Serial.print(F("  배정 X1=드라이버")); Serial.print(AXIS_X1_DRV);
  Serial.print(F(" X2=드라이버")); Serial.print(AXIS_X2_DRV);
  Serial.print(F(" Y=드라이버"));  Serial.print(AXIS_Y_DRV);
  Serial.print(F(" Z=드라이버"));  Serial.print(AXIS_Z_DRV);
  Serial.print(F("  RELAY_ACTIVE_LOW=")); Serial.println(RELAY_ACTIVE_LOW ? 1 : 0);

  assignOk = checkAssign();
  if (assignOk) {
    cacheMotorPins();
    Serial.print(F("  PUL"));
    for (uint8_t m = 0; m < N_MOTOR; m++) { Serial.print(' '); printMotorPul(m); }
    Serial.println();
  }

  int r = eeLoad();
  if (r == 1)      Serial.println(F("  EEPROM: 마지막 저장 위치 복원  (꺼진 동안 손으로 밀었다면 fz 로 재등록)"));
  else if (r == 2) Serial.println(F("  EEPROM: save 하지 않고 꺼짐 → 위치 불신"));
  else             Serial.println(F("  EEPROM: 기록 없음"));
  printStatus();
}

void loop() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  String tok[3]; int nTok = 0; int st = 0;
  for (int i = 0; i <= (int)line.length() && nTok < 3; i++) {
    if (i == (int)line.length() || line.charAt(i) == ' ') {
      if (i > st) tok[nTok++] = line.substring(st, i);
      st = i + 1;
    }
  }
  String c = tok[0]; c.toLowerCase();
  String s1 = tok[1]; s1.toLowerCase();
  long  a1 = (nTok > 1) ? tok[1].toInt()   : 0;
  long  a2 = (nTok > 2) ? tok[2].toInt()   : 0;
  float f1 = (nTok > 1) ? tok[1].toFloat() : 0.0;

  abortFlag = false;

  if (isMotionCmd(c) && rejectIfBadAssign()) return;

  // ---- 원점 / 위치 세팅 ----
  if (c == "fz") {
    if      (s1 == "x") findZero(AX_X, a2);
    else if (s1 == "y") findZero(AX_Y, a2);
    else if (s1 == "z") findZero(AX_Z, a2);   // Z 는 0 쪽 = 위
    else Serial.println(F("  ? fz x [탐색펄스]  (x / y / z. 기본 800 = 5mm, 상한 1600)"));
  }
  else if (c == "z")  { posX1 = 0; posX2 = 0; posY = 0; homedX = true; homedY = true;
                        eeWrite(true); Serial.println(F("  X, Y 여기를 0 으로 등록 (저장됨)")); printStatus(); }
  else if (c == "zx") { posX1 = 0; posX2 = 0; homedX = true;
                        eeWrite(true); Serial.println(F("  X 여기를 0 으로 등록 (저장됨)")); printStatus(); }
  else if (c == "zy") { posY = 0; homedY = true;
                        eeWrite(true); Serial.println(F("  Y 여기를 0 으로 등록 (저장됨)")); printStatus(); }
  else if (c == "zz") { posZ = 0; homedZ = true;
                        eeWrite(true); Serial.println(F("  Z 여기를 0 으로 등록 (저장됨)")); printStatus(); }
  else if (c == "sp") {                              // PC가 알려준 위치
    if (nTok < 3) Serial.println(F("  ? sp <x> <y>"));
    else if (a1 < 0 || a1 > X_MAX || a2 < 0 || a2 > Y_MAX)
      Serial.println(F("  [거부] sp 값이 범위 밖"));
    else {
      posX1 = a1; posX2 = a1; posY = a2; homedX = true; homedY = true;
      eeWrite(true);
      Serial.println(F("  PC 위치로 세팅 (저장됨)")); printStatus();
    }
  }
  else if (c == "home") {
    if (requireHomed(AX_X) && requireHomed(AX_Y)) {
      gotoAbs(AX_X, 0);
      if (!abortFlag) { delay(pauseMs); gotoAbs(AX_Y, 0); }
    }
  }

  // ---- 절대 이동 ----
  else if (c == "gx") gotoAbs(AX_X, a1);
  else if (c == "gy") gotoAbs(AX_Y, a1);
  else if (c == "gz") gotoAbs(AX_Z, a1);
  else if (c == "mx") gotoAbs(AX_X, (long)(f1 * PPMM + 0.5));
  else if (c == "my") gotoAbs(AX_Y, (long)(f1 * PPMM + 0.5));
  else if (c == "mz") gotoAbs(AX_Z, (long)(f1 * Z_PPMM + 0.5));
  else if (c == "g") {
    if (requireHomed(AX_X) && requireHomed(AX_Y)
        && checkLimit(AX_X, a1) && checkLimit(AX_Y, a2)) {
      gotoAbs(AX_X, a1);
      if (!abortFlag) { delay(pauseMs); gotoAbs(AX_Y, a2); }
    }
  }

  // ---- 상대 이동 ----
  else if (c == "jx") jogRel(AX_X, a1);        // 원점 없어도 됨
  else if (c == "jy") jogRel(AX_Y, a1);        // 원점 없어도 됨
  else if (c == "jz") jogRel(AX_Z, a1);        // 원점 없어도 됨 (- = 위)
  else if (c == "x")  moveRel(AX_X, a1);
  else if (c == "y")  moveRel(AX_Y, a1);
  else if (c == "x1") moveSingle(AX_X1, a1);
  else if (c == "x2") moveSingle(AX_X2, a1);

  // ---- 테스트 ----
  else if (c == "rx") { if (a1 && a2 > 0) reciprocate(AX_X, labs2(a1), a2); }
  else if (c == "ry") { if (a1 && a2 > 0) reciprocate(AX_Y, labs2(a1), a2); }
  else if (c == "fx") { if (a1 && a2 > 0) repeatSame(AX_X, labs2(a1), a2, a1 >= 0); }
  else if (c == "fy") { if (a1 && a2 > 0) repeatSame(AX_Y, labs2(a1), a2, a1 >= 0); }

  // ---- 릴레이 / 호밍 센서 ----
  else if (c == "r")  relayCmd(s1, tok[2], nTok);
  else if (c == "rs") printRS();
  else if (c == "hs") { homeSample(); printHS(); }
  else if (c == "hw") homeWatch();

  // ---- 저장 / 상태 ----
  else if (c == "save")   { eeWrite(true); Serial.println(F("  저장됨 (신뢰 확정)")); }
  else if (c == "forget") { homedX = false; homedY = false; homedZ = false; eeWrite(true);   // 릴레이는 그대로
                            Serial.println(F("  기록 삭제")); printStatus(); }
  else if (c == "st")     printST();
  else if (c == "p")      { printStatus(); printWiring(); }

  // ---- 파라미터 / 기타 ----
  else if (c == "v") { if (a1 > 0) vMax   = a1; printStatus(); }
  else if (c == "a") { if (a1 > 0) accel  = a1; printStatus(); }
  else if (c == "b") { if (a1 > 0) vStart = a1; printStatus(); }
  else if (c == "w") { pauseMs = a1; printStatus(); }
  else if (c == "e") {
    if (a1) {
      motorsEnable(false);
      homedX = false; homedY = false; homedZ = false; eeWrite(true);
      Serial.println(F("  모터 풀림 - 위치 신뢰 해제. 다시 움직이려면 fz 또는 sp 필요"));
    } else { motorsOn(); Serial.println(F("  모터 켜짐")); }
  }
  else if (c == "!") Serial.println(F("  (이동 중에만 유효)"));   // 릴레이는 건드리지 않는다
  else {
    Serial.println(F("  ? fz z zx zy zz sp home gx gy gz g mx my mz jx jy jz x y x1 x2 rx ry fx fy save forget st p v a b w e !"));
    Serial.println(F("    jx/jy/jz <±펄스> : 원점 없어도 되는 이동. 범위 밖으로 나가면 그 축 원점 해제"));
    Serial.println(F("    Z: 0 = 맨 위, + = 아래. 원점은 위쪽 하드스톱 (fz z)"));
    Serial.println(F("    r <1~10> <1|0> / r all 0 / rs : 릴레이    hs / hw : 호밍 센서 (hw 는 아무 입력이나 오면 끝)"));
  }
}
