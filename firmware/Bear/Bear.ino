// Teddy (OwlHacks 2026) -- bear body firmware.
// 8 SG90 servos on pins 2..9 (servo id N is on pin N+2). Host does all the
// easing; this just sets angles and manages attach/detach.
//
// Serial protocol @115200, one command per line:
//   "<id> <angle>"  ->  set servo <id> (0..7) to <angle> (0..180).
//                       Attaches the servo first if it was detached.
//   "D <id>"        ->  detach one servo, so it stops holding and stops
//                       buzzing. The legs get this after every move.
//   "R"             ->  relax: detach every servo
//   "A"             ->  re-attach every servo at its last angle
//   "P"             ->  ping, replies "BEAR OK"
// Replies "BEAR READY" once on boot.

#include <Servo.h>

const uint8_t PINS[8] = {2, 3, 4, 5, 6, 7, 8, 9};

// Where each servo sits at rest. The arms hang outward at 160/20; swinging
// them back toward 90 fouls the legs, so that is the far end of their travel.
// head_tilt rests at 110, not 90: the webcam in his hat is heavy enough to
// pull his chin down, so he holds it 20 degrees up.
const uint8_t HOME[8] = {90, 110, 160, 20, 90, 90, 90, 90};

// Legs jitter audibly when they hold position, and one of them is glued in,
// so they are detached whenever they are not actually moving.
const uint8_t FIRST_LEG = 4;

Servo servos[8];
uint8_t lastAngle[8];
bool isAttached[8];

char buf[16];
uint8_t len = 0;

void attachOne(uint8_t i) {
  if (isAttached[i]) return;
  servos[i].attach(PINS[i]);
  servos[i].write(lastAngle[i]);
  isAttached[i] = true;
}

void detachOne(uint8_t i) {
  if (!isAttached[i]) return;
  servos[i].detach();
  isAttached[i] = false;
}

void setup() {
  Serial.begin(115200);
  for (uint8_t i = 0; i < 8; i++) {
    lastAngle[i] = HOME[i];
    isAttached[i] = false;
    attachOne(i);
  }
  delay(800);                                   // let everyone reach home
  for (uint8_t i = FIRST_LEG; i < 8; i++) detachOne(i);   // then hush the legs
  Serial.println("BEAR READY");
}

void handleLine() {
  if (len == 0) return;

  if (buf[0] == 'R' || buf[0] == 'r') {
    for (uint8_t i = 0; i < 8; i++) detachOne(i);
    return;
  }
  if (buf[0] == 'A' || buf[0] == 'a') {
    for (uint8_t i = 0; i < 8; i++) attachOne(i);
    return;
  }
  if (buf[0] == 'P' || buf[0] == 'p') { Serial.println("BEAR OK"); return; }
  if (buf[0] == 'D' || buf[0] == 'd') {
    int id = atoi(buf + 1);
    if (id >= 0 && id <= 7) detachOne((uint8_t)id);
    return;
  }

  // "<id> <angle>"
  char *sp = strchr(buf, ' ');
  if (!sp) return;
  *sp = '\0';
  int id = atoi(buf);
  int angle = atoi(sp + 1);
  if (id < 0 || id > 7) return;
  if (angle < 0) angle = 0;
  if (angle > 180) angle = 180;

  lastAngle[id] = (uint8_t)angle;
  attachOne((uint8_t)id);          // no-op if already attached
  servos[id].write(angle);
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      buf[len] = '\0';
      handleLine();
      len = 0;
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }
}
