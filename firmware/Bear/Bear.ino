// Teddy (OwlHacks 2026) -- bear body firmware.
// 8 SG90 servos on pins 2..9. Host does all the easing; this just sets angles.
//
// Serial protocol @115200, one command per line:
//   "<id> <angle>"  ->  set servo <id> (0..7) to <angle> (0..180)
//   "R"             ->  relax: detach every servo (stops buzzing, saves the AAs)
//   "A"             ->  re-attach every servo at its last angle
//   "P"             ->  ping, replies "BEAR OK"
// Replies "BEAR READY" once on boot.

#include <Servo.h>

const uint8_t PINS[8] = {2, 3, 4, 5, 6, 7, 8, 9};
Servo servos[8];
uint8_t lastAngle[8];
bool attached = false;

char buf[16];
uint8_t len = 0;

void attachAll() {
  for (uint8_t i = 0; i < 8; i++) {
    servos[i].attach(PINS[i]);
    servos[i].write(lastAngle[i]);
  }
  attached = true;
}

void detachAll() {
  for (uint8_t i = 0; i < 8; i++) servos[i].detach();
  attached = false;
}

void setup() {
  Serial.begin(115200);
  for (uint8_t i = 0; i < 8; i++) lastAngle[i] = 90;
  attachAll();
  Serial.println("BEAR READY");
}

void handleLine() {
  if (len == 0) return;

  if (buf[0] == 'R' || buf[0] == 'r') { detachAll(); return; }
  if (buf[0] == 'A' || buf[0] == 'a') { attachAll(); return; }
  if (buf[0] == 'P' || buf[0] == 'p') { Serial.println("BEAR OK"); return; }

  // "<id> <angle>"
  char *sp = strchr(buf, ' ');
  if (!sp) return;
  *sp = '\0';
  int id = atoi(buf);
  int angle = atoi(sp + 1);
  if (id < 0 || id > 7) return;
  if (angle < 0) angle = 0;
  if (angle > 180) angle = 180;

  if (!attached) attachAll();
  lastAngle[id] = (uint8_t)angle;
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
