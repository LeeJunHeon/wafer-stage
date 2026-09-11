//====================================================================
// 3축 스텝모터 제어 V7 — 절대좌표 + 원점 + 소프트리밋 + 위치 기억(세션 단위)
//
// V6 에서 바뀐 것만:
//   jx/jy  원점이 없어도 되는 상대 이동(한 번에 8000 펄스 = 50mm 까지).
//          사람이 보면서 끝단까지 몰고 가 zx/zy 로 원점을 등록하는 절차용이다.
//   fz     인자 없으면 800 펄스(5mm)만 탐색한다(상한 1600 = 10mm). V6 는 전체를 밀어,
//          이미 끝에 있으면 33초를 갈았다(2026-09-11).
// 그 밖의 명령·출력 형식은 V6 와 같다.
// Arduino Mega 2560 + MotorBank MSD-224 x3
//
//   X1 : PUL=D2  DIR=D31  ENA=D30
//   X2 : PUL=D3  DIR=D49  ENA=D48
//   Y1 : PUL=D4  DIR=D53  ENA=D52
//
//   160 펄스 = 1mm
//   X 스트로크 39,620 펄스 (247.6mm)   Y 스트로크 39,640 펄스 (247.8mm)
//
// 시리얼: 115200 bps, 줄 끝 = "새 줄". 명령어와 숫자 사이 띄어쓰기 필수.
//
// [핵심 규칙]  원점이 없으면 fz 와 jx/jy 외에는 아무것도 움직이지 않는다.
//
// [위치 기억]  위치는 RAM. EEPROM에는 "저장 이후 움직였는가"만 표시.
//   첫 이동 시  ok=0 기록 (세션에 1회)   /   save 시  ok=1 기록
//   전원 켤 때  ok=1 이면 복원, ok=0 이면 원점없음 (save 안 하고 꺼진 것)
//   → 세션당 EEPROM 쓰기 2회. 수명 걱정 없음.
//   파이썬이 메인이면: 이동 보고를 파일에 기록하고, 파킹/종료 때 save 전송.
//
// [원점]
//   fz x [탐색펄스] / fz y [탐색펄스]   - 그만큼 밀고 2mm 이격 후 0 (기본 800 = 5mm, 상한 1600)
//   z / zx / zy      지금 위치를 0 으로 등록 (움직이지 않음)
//   sp x y           PC가 알려준 위치로 세팅 + 원점OK  (파이썬 복구용)
//   home             (0,0) 으로 복귀
//
// [절대 이동]  gx 16000   gy 8000   g 16000 8000   mx 100.0   my 50.5
// [상대 이동]  x 1600   y -1600   x1 200   x2 200
// [수동 이동]  jx 1600   jy -1600      (원점 없어도 됨. 한 번에 8000 펄스까지)
// [테스트]     rx 5000 10   ry 5000 10   fx 5000 5   fy 5000 5
// [상태]       p  (사람용)     st  (파싱용 한 줄: ST X= Y= X2= HX= HY= DIRTY=)
// [기타]       save   forget   v a b w   e 0/1   !
//====================================================================
#include <EEPROM.h>

const uint8_t X1_PUL = 2,  X1_DIR = 31, X1_ENA = 30;
const uint8_t X2_PUL = 3,  X2_DIR = 49, X2_ENA = 48;
const uint8_t Y1_PUL = 4,  Y1_DIR = 53, Y1_ENA = 52;

const uint8_t M_X1 = 0x10, M_X2 = 0x20, M_X = 0x30;   // PORTE
const uint8_t M_Y  = 0x20;                            // PORTG

const long  X_MAX    = 39620;
const long  Y_MAX    = 39640;
const float PPMM     = 160.0;
const long  HOME_GAP = 320;

const bool X2_DIR_INVERT   = false;
const long BACKLASH_TAKEUP = 0;

const uint16_t PULSE_HIGH_US = 5;
const uint16_t DIR_SETUP_US  = 50;

float    vMax = 6000.0, accel = 40000.0, vStart = 300.0;
uint16_t pauseMs = 200;

long posX1 = 0, posX2 = 0, posY = 0;
bool homedX = false, homedY = false;
bool abortFlag = false;
bool eeDirty   = false;     // 저장 이후 이동이 있었는가 (RAM)

enum Axis { AX_X, AX_Y, AX_X1, AX_X2 };

// ==================================================================
//  EEPROM — 슬롯 1개, 세션당 2회 쓰기
// ==================================================================
struct PosRec {
  uint32_t magic;
  int32_t  x1, x2, y;
  uint8_t  homedX, homedY;
  uint8_t  ok;       // 1 = 저장 이후 이동 없음 (신뢰)  0 = 이동 있었음 (불신)
  uint8_t  sum;
};
const uint32_t EE_MAGIC = 0x504F5336UL;   // "POS6"
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
  r.x1 = posX1; r.x2 = posX2; r.y = posY;
  r.homedX = homedX ? 1 : 0;
  r.homedY = homedY ? 1 : 0;
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
  posX1 = r.x1; posX2 = r.x2; posY = r.y;
  homedX = r.homedX; homedY = r.homedY;
  return 1;
}

void markDirty() { if (!eeDirty) eeWrite(false); }   // 세션 첫 이동에만 실제 쓰기

// ==================================================================
long  axPos(Axis ax)   { return (ax == AX_Y) ? posY : posX1; }
long  axMax(Axis ax)   { return (ax == AX_Y) ? Y_MAX : X_MAX; }

// V7 추가 상수
const long HOME_SEARCH_DEFAULT = 800;    // fz 인자 없을 때 5mm 만 민다
const long HOME_SEARCH_MAX     = 1600;   // fz 탐색 상한 10mm (넘으면 거부)
const long JOG_MAX_PULSE       = 8000;   // jx/jy 한 번에 50mm 까지
bool  axHomed(Axis ax) { return (ax == AX_Y) ? homedY : homedX; }
long  labs2(long v)    { return v < 0 ? -v : v; }

const __FlashStringHelper* axName(Axis ax) {
  switch (ax) {
    case AX_X:  return F("X ");
    case AX_X1: return F("X1");
    case AX_X2: return F("X2");
    default:    return F("Y ");
  }
}

void motorsOn() {
  digitalWrite(X1_ENA, LOW); digitalWrite(X2_ENA, LOW); digitalWrite(Y1_ENA, LOW);
}

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

  if (posX1 != posX2) {
    Serial.print(F("  [!] X 틀어짐  X1=")); Serial.print(posX1);
    Serial.print(F(" X2=")); Serial.println(posX2);
  }
  if (!homedX || !homedY)
    Serial.println(F("  → 원점이 없는 축은 움직이지 않습니다. fz x / fz y 또는 sp x y"));
}

// 파싱용 한 줄
void printST() {
  Serial.print(F("ST X="));  Serial.print(posX1);
  Serial.print(F(" Y="));    Serial.print(posY);
  Serial.print(F(" X2="));   Serial.print(posX2);
  Serial.print(F(" HX="));   Serial.print(homedX ? 1 : 0);
  Serial.print(F(" HY="));   Serial.print(homedY ? 1 : 0);
  Serial.print(F(" DIRTY=")); Serial.println(eeDirty ? 1 : 0);
}

bool requireHomed(Axis ax) {
  if (axHomed(ax)) return true;
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
    case AX_X:  digitalWrite(X1_DIR, pos); digitalWrite(X2_DIR, d2); break;
    case AX_X1: digitalWrite(X1_DIR, pos); break;
    case AX_X2: digitalWrite(X2_DIR, d2);  break;
    case AX_Y:  digitalWrite(Y1_DIR, pos); break;
  }
  delayMicroseconds(DIR_SETUP_US);
}

void getMask(Axis ax, uint8_t &mE, uint8_t &mG) {
  mE = 0; mG = 0;
  switch (ax) {
    case AX_X:  mE = M_X;  break;
    case AX_X1: mE = M_X1; break;
    case AX_X2: mE = M_X2; break;
    case AX_Y:  mG = M_Y;  break;
  }
}

void addPos(Axis ax, long d) {
  switch (ax) {
    case AX_X:  posX1 += d; posX2 += d; break;
    case AX_X1: posX1 += d; break;
    case AX_X2: posX2 += d; break;
    case AX_Y:  posY  += d; break;
  }
}

// ---- 실제 펄스 출력 (검사 없음. 상위에서 검사) ----
void moveAxis(Axis ax, unsigned long steps, bool pos) {
  if (steps == 0) return;
  markDirty();

  uint8_t mE, mG;
  getMask(ax, mE, mG);
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

    PORTE |= mE;  PORTG |= mG;
    delayMicroseconds(PULSE_HIGH_US);
    PORTE &= ~mE; PORTG &= ~mG;
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
// 사람이 보면서 캐리지를 끝단까지 몰고 가는 용도다. 원점이 있으면 소프트리밋을
// 그대로 지키고, 없으면 범위를 볼 기준이 없으므로 검사하지 않는다. 한 번에 갈 수
// 있는 거리를 제한해 '원점 없이 크게 보내는' 사고를 막는다.
void jogRel(Axis ax, long delta) {
  if (delta == 0) return;
  if (labs2(delta) > JOG_MAX_PULSE) {
    Serial.println(F("  [거부] jx/jy 는 한 번에 8000 펄스까지"));
    return;
  }
  if (axHomed(ax) && !checkLimit(ax, axPos(ax) + delta)) return;
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
  bool  hx = homedX, hy = homedY;
  homedX = false; homedY = false;
  vMax = 1200.0; accel = 20000.0;

  moveAxis(ax, searchLen, false);
  delay(500);
  if (!abortFlag) moveAxis(ax, HOME_GAP, true);

  vMax = sv; accel = sa;
  homedX = hx; homedY = hy;

  if (abortFlag) { Serial.println(F("--- 중단됨. 원점 설정 안 됨 ---")); return; }
  if (ax == AX_X) { posX1 = 0; posX2 = 0; homedX = true; }
  else            { posY = 0; homedY = true; }
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

// ------------------------------------------------------------------
void setup() {
  const uint8_t outs[] = { X1_PUL, X1_DIR, X1_ENA,
                           X2_PUL, X2_DIR, X2_ENA,
                           Y1_PUL, Y1_DIR, Y1_ENA };
  for (uint8_t i = 0; i < 9; i++) { pinMode(outs[i], OUTPUT); digitalWrite(outs[i], LOW); }

  Serial.begin(115200);
  Serial.setTimeout(30);
  Serial.println(F("=== 3-AXIS V7 ==="));
  Serial.print(F("  X 0~")); Serial.print(X_MAX);
  Serial.print(F("  Y 0~")); Serial.print(Y_MAX);
  Serial.println(F("   (160 pulse = 1mm)"));

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

  // ---- 원점 / 위치 세팅 ----
  if (c == "fz") {
    if      (s1 == "x") findZero(AX_X, a2);
    else if (s1 == "y") findZero(AX_Y, a2);
    else Serial.println(F("  ? fz x [탐색펄스]  또는  fz y [탐색펄스]  (기본 800 = 5mm, 상한 1600)"));
  }
  else if (c == "z")  { posX1 = 0; posX2 = 0; posY = 0; homedX = true; homedY = true;
                        eeWrite(true); Serial.println(F("  X, Y 여기를 0 으로 등록 (저장됨)")); printStatus(); }
  else if (c == "zx") { posX1 = 0; posX2 = 0; homedX = true;
                        eeWrite(true); Serial.println(F("  X 여기를 0 으로 등록 (저장됨)")); printStatus(); }
  else if (c == "zy") { posY = 0; homedY = true;
                        eeWrite(true); Serial.println(F("  Y 여기를 0 으로 등록 (저장됨)")); printStatus(); }
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
  else if (c == "mx") gotoAbs(AX_X, (long)(f1 * PPMM + 0.5));
  else if (c == "my") gotoAbs(AX_Y, (long)(f1 * PPMM + 0.5));
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
  else if (c == "x")  moveRel(AX_X, a1);
  else if (c == "y")  moveRel(AX_Y, a1);
  else if (c == "x1") moveSingle(AX_X1, a1);
  else if (c == "x2") moveSingle(AX_X2, a1);

  // ---- 테스트 ----
  else if (c == "rx") { if (a1 && a2 > 0) reciprocate(AX_X, labs2(a1), a2); }
  else if (c == "ry") { if (a1 && a2 > 0) reciprocate(AX_Y, labs2(a1), a2); }
  else if (c == "fx") { if (a1 && a2 > 0) repeatSame(AX_X, labs2(a1), a2, a1 >= 0); }
  else if (c == "fy") { if (a1 && a2 > 0) repeatSame(AX_Y, labs2(a1), a2, a1 >= 0); }

  // ---- 저장 / 상태 ----
  else if (c == "save")   { eeWrite(true); Serial.println(F("  저장됨 (신뢰 확정)")); }
  else if (c == "forget") { homedX = false; homedY = false; eeWrite(true);
                            Serial.println(F("  기록 삭제")); printStatus(); }
  else if (c == "st")     printST();
  else if (c == "p")      printStatus();

  // ---- 파라미터 / 기타 ----
  else if (c == "v") { if (a1 > 0) vMax   = a1; printStatus(); }
  else if (c == "a") { if (a1 > 0) accel  = a1; printStatus(); }
  else if (c == "b") { if (a1 > 0) vStart = a1; printStatus(); }
  else if (c == "w") { pauseMs = a1; printStatus(); }
  else if (c == "e") {
    if (a1) {
      digitalWrite(X1_ENA, HIGH); digitalWrite(X2_ENA, HIGH); digitalWrite(Y1_ENA, HIGH);
      homedX = false; homedY = false; eeWrite(true);
      Serial.println(F("  모터 풀림 - 위치 신뢰 해제. 다시 움직이려면 fz 또는 sp 필요"));
    } else { motorsOn(); Serial.println(F("  모터 켜짐")); }
  }
  else if (c == "!") Serial.println(F("  (이동 중에만 유효)"));
  else Serial.println(F("  ? fz z zx zy sp home gx gy g mx my jx jy x y x1 x2 rx ry fx fy save forget st p v a b w e !"));
}
