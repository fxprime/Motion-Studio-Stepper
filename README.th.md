# ModuleMore Motion Studio — PD42S1

[English](README.md) · ภาษาไทย

[![ModuleMore Motion Studio: เห็นการเคลื่อนไหว ทดลองควบคุมได้ด้วยตัวเอง](docs/assets/motion-studio-banner.svg)](https://www.modulemore.com/product/2932/pd42s1-foc-closed-loop-stepper-driver-rs485)

โปรแกรมสาธิตและทดลองไดรเวอร์สเต็ปเปอร์มอเตอร์วงปิด **PD42S1** ผ่านเว็บเบราว์เซอร์บนเครื่องของคุณ เลือกควบคุมความเร็ว แรงบิดด้วยกระแส หรือตำแหน่ง พร้อมกราฟสด แสดงมุมโรเตอร์ ตั้งศูนย์ สั่งคาลิเบรต และทำ Homing ได้ในหน้าเดียว

หน้าจอโปรแกรมเป็นภาษาอังกฤษ เอกสารติดตั้งและใช้งานมีทั้งภาษาไทยและภาษาอังกฤษ ไม่ต้องติดตั้ง Node.js หรือ build หน้าเว็บ และมีโหมดจำลองให้ลองก่อนต่อมอเตอร์จริง

**รายละเอียดสินค้า / สั่งซื้อ:** [PD42S1 FOC Closed-loop Stepper Driver RS485 — ModuleMore](https://www.modulemore.com/product/2932/pd42s1-foc-closed-loop-stepper-driver-rs485)

## ดูหน้าจอและกราฟตัวอย่าง

![หน้าจอ Motion Studio พร้อมกราฟตำแหน่งและมุมโรเตอร์ในโหมดจำลอง](docs/assets/showcase-overview.png)

| กราฟตำแหน่ง | กราฟกระแส |
| --- | --- |
| ![เป้าหมายและตำแหน่งโรเตอร์จากตัวจำลอง](docs/assets/position-tracking.png) | ![คำสั่งกระแสและการปิดเอาต์พุตเมื่อครบเวลาจากตัวจำลอง](docs/assets/current-response.png) |

ภาพหน้าจอและกราฟทั้งหมดด้านบนมาจาก **โหมดจำลอง** ไม่ใช่ผลวัดสมรรถนะมอเตอร์จริง ส่วนโรเตอร์และเส้นกราฟในแบนเนอร์เป็นภาพประกอบ ผลใช้งานจริงขึ้นอยู่กับมอเตอร์ โหลด แหล่งจ่าย การจูน และการสื่อสาร

## การติดตั้ง

### สิ่งที่ต้องมี

- Python **3.10 ขึ้นไป** พร้อม `pip` และ Git
- เว็บเบราว์เซอร์รุ่นปัจจุบัน
- โค้ดจาก repository นี้และไลบรารี [pd42s1-motor](https://github.com/fxprime/pd42s1-motor)
- ถ้าใช้ฮาร์ดแวร์จริง: PD42S1 พร้อมแหล่งจ่าย มอเตอร์ที่เข้ากันได้ และสาย USB หรืออะแดปเตอร์ USB-to-TTL/RS485 ที่เหมาะสม ตรวจสอบแรงดันและการต่อสายจากเอกสารสินค้า

### macOS / Linux

เปิด Terminal ในโฟลเดอร์ที่ต้องการเก็บโปรเจกต์ แล้วรัน:

```sh
git clone https://github.com/fxprime/pd42s1-motor.git
git clone https://github.com/fxprime/Motion-Studio-Stepper.git
cd Motion-Studio-Stepper
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ../pd42s1-motor
python -m pip install -e .
pd42s1-showcase
```

### Windows (PowerShell)

เปิด PowerShell ในโฟลเดอร์ที่ต้องการเก็บโปรเจกต์ คำสั่งนี้ไม่ต้อง activate environment:

```powershell
git clone https://github.com/fxprime/pd42s1-motor.git
git clone https://github.com/fxprime/Motion-Studio-Stepper.git
cd Motion-Studio-Stepper
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ..\pd42s1-motor
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m showcase
```

คำสั่งติดตั้งสมมติว่า repository ทั้งสองอยู่ข้างกัน หากไลบรารีอยู่ที่อื่น ให้เปลี่ยนพาธ `../pd42s1-motor` หรือ `..\pd42s1-motor` ให้ตรงกับเครื่องของคุณ

### เปิดใช้งานครั้งถัดไป

เปิด [http://127.0.0.1:8422](http://127.0.0.1:8422) โปรแกรมจะเปิดเบราว์เซอร์ให้อัตโนมัติตามปกติ ต้องเปิด Terminal ของ server ค้างไว้ ใช้ `Ctrl+C` เพื่อปิดโปรแกรมตามปกติ

- macOS: หลังติดตั้งแล้ว ดับเบิลคลิก `run.command` เพื่อใช้ฮาร์ดแวร์จริง หรือ `preview.command` เพื่อจำลอง
- macOS/Linux: รัน `./.venv/bin/python -m showcase` จากโฟลเดอร์โปรเจกต์
- Windows: รัน `.\.venv\Scripts\python.exe -m showcase`

ถ้าพอร์ตถูกใช้งาน สามารถเปลี่ยนเป็นพอร์ตอื่นได้ เช่น `python -m showcase --port 8423 --no-browser` แล้วเปิด `http://127.0.0.1:8423`

## ทดลองก่อนต่อมอเตอร์จริง

หลัง activate environment ตามขั้นตอนติดตั้ง ใช้:

```sh
python -m showcase --preview
```

ใน Windows ใช้ `.\.venv\Scripts\python.exe -m showcase --preview` เลือกอุปกรณ์ **PREVIEW** แล้วกด **Connect controller** โหมดนี้ไม่เปิดพอร์ต serial ใด ๆ และหน้าจอระบุว่าเป็นฮาร์ดแวร์จำลอง

เมื่อต้องการใช้มอเตอร์จริง ให้ปิด server จำลองก่อน แล้วเปิดใหม่โดย **ไม่ใส่ `--preview`** จากนั้นรีเฟรชเบราว์เซอร์ การเสียบมอเตอร์ไม่ได้เปลี่ยน server จำลองเป็น server ฮาร์ดแวร์จริงโดยอัตโนมัติ

## วิธีใช้งาน

### เชื่อมต่อ

1. ต่อมอเตอร์ แหล่งจ่าย และสายสื่อสารตามคู่มือ ตรวจสอบพื้นที่การเคลื่อนไหวให้ปลอดภัย
2. ปิดการเชื่อมต่อจากโปรแกรม serial อื่นก่อน รวมถึงโปรแกรม engineering เพราะหนึ่งพอร์ตควรมีโปรแกรมควบคุมเพียงตัวเดียว
3. เลือก **USB**, **TTL UART** หรือ **RS485**, พอร์ตอุปกรณ์ และ baud rate ที่ตรงกับไดรเวอร์ สำหรับ RS485 ให้เลือก address ที่ถูกต้องด้วย
4. กด **Connect controller** แล้วเลือกโหมด ตั้งค่า และยืนยันก่อนเริ่มการเคลื่อนไหว

### โหมดควบคุม

| ฟังก์ชัน | วิธีใช้ / ข้อควรทราบ |
| --- | --- |
| **Velocity** | ตั้งความเร็วหน่วย RPM ค่าลบคือทิศกลับ มี preset และปุ่ม Reverse |
| **Torque** | ตั้งคำสั่งกระแสหน่วย mA ไม่ใช่แรงบิดที่วัดเป็น N·m ค่าสัมบูรณ์ต้องไม่เกิน Current limit โหมดนี้ไม่ควบคุมความเร็ว มอเตอร์ไร้โหลดอาจเร่งเร็วมาก |
| **Position** | Relative distance คือระยะจากตำแหน่งปัจจุบัน ส่วน Absolute position คือพิกัดอ้างอิงจากจุดศูนย์และต้องไม่ติดลบ ตั้งความเร็วและระยะเวลาด้วย |
| **Set zero** | หยุด/ปิดเอาต์พุตก่อน แล้วตั้งตำแหน่งปัจจุบันเป็นศูนย์สำหรับคำสั่งครั้งถัดไป ไม่ใช่การค้นหา home ทางกายภาพ |
| **Calibrate encoder** | ใช้เมื่อถอดโหลดและโรเตอร์หมุนได้อย่างอิสระ มอเตอร์จะหมุนระหว่างคาลิเบรต ต้องรอ LCD ของไดรเวอร์ยืนยันว่าเสร็จ การตอบรับคำสั่งไม่ได้แปลว่าคาลิเบรตเสร็จแล้ว |
| **Homing** | เลือก limit switch หรือ sensorless stop, strategy, ทิศ และความเร็ว ต้องมี reference ทางกายภาพที่ถูกต้อง ใช้ Current limit เป็น threshold กระแส และ Test duration เป็น timeout |
| **Clear stall** | หยุด/ปิดเอาต์พุตแล้วล้างสถานะ stall ต้องแก้สาเหตุของการติดขัดก่อน |

หนึ่งรอบเท่ากับ **51,200 controller units** เช่น 3,200 units = 1/16 รอบ และ 6,400 units = 1/8 รอบ ปุ่ม preset ระยะจะเลือกการเคลื่อนที่แบบ relative เสมอ

เริ่มจากค่าต่ำและระยะเวลาสั้นที่เหมาะกับชุดมอเตอร์ของคุณ ค่าเริ่มต้นของ UI คือ 30 RPM, Current limit 300 mA และระยะเวลา 5 วินาที แต่ไม่ใช่การรับประกันความปลอดภัยสำหรับทุกกลไก

### กราฟและการหยุด

- เลือกแท็บ **Velocity / Current / Position** เพื่อดูข้อมูลย้อนหลัง 12 วินาที และกด **Export CSV** เพื่อบันทึกตัวอย่างข้อมูล
- ระบบพยายามอ่าน telemetry ที่ 50 Hz โดยแสดงอัตราที่ทำได้จริงในหน้าจอ อัตราขึ้นอยู่กับไดรเวอร์ baud rate และ transport
- การทดสอบ motion ตั้งเวลาได้ 1–30 วินาที เมื่อครบเวลาจะหยุดและสั่ง disable พร้อมตรวจสอบ read-back
- กด **Stop & disable** เพื่อหยุดก่อนครบเวลา หรือ **Disconnect** เพื่อหยุด/disable ก่อนปิดพอร์ต
- การหมดเวลาไม่แปลว่าตำแหน่งถึงเป้าหมายหรือ Homing สำเร็จ ต้องตรวจสอบพิกัดและ error ปัจจุบัน โปรแกรมยังไม่ได้ poll คำสั่งสถานะ Homing `0x96`

## ความปลอดภัย

- Torque ควบคุมกระแส ไม่ใช่ความเร็ว ต้องพร้อมหยุดและมีระบบตัดกำลังอิสระตามความเสี่ยงของเครื่อง
- Software stop ไม่ใช่ emergency stop ทางกายภาพ การสื่อสารขาดหายหรือ server crash อาจทำให้ส่ง disable ไม่ได้
- Timer ทำงานใน Python server การปิดแท็บเบราว์เซอร์ไม่ได้ยกเลิก timer และมี idle-disable ตั้งไว้ 60 วินาทีจาก shared core
- หาก enable/disable read-back ล้มเหลว อย่าถือว่ามอเตอร์เปลี่ยนสถานะแล้ว หยุดทดสอบและตรวจสอบระบบก่อน
- โปรแกรมปรับ current limit และพารามิเตอร์ Homing บางรายการ แต่ไม่ได้ส่งคำสั่งบันทึกลง flash ตรวจสอบการตั้งค่าก่อนกลับไปใช้งานปกติ
- Server รับการเชื่อมต่อเฉพาะ `127.0.0.1` ไม่มีระบบยืนยันตัวตนสำหรับการใช้งานบนอินเทอร์เน็ต ไม่ควรเปิดสู่เครือข่ายสาธารณะ

## แก้ปัญหาเบื้องต้น

- **หาอุปกรณ์ไม่พบ:** ตรวจสอบแหล่งจ่าย สายข้อมูล USB/อะแดปเตอร์ driver และสิทธิ์พอร์ต แล้วกด Refresh devices
- **เชื่อมต่อไม่ได้ / พอร์ตไม่ว่าง:** ปิดโปรแกรมที่ใช้พอร์ตเดียวกัน ตรวจสอบ baud และ RS485 address / A / B / common ground
- **ยังแสดง Simulation:** เปิด server ใหม่โดยไม่ใส่ `--preview` แล้วรีเฟรชหน้า
- **กราฟหยุดหลัง restart:** รีเฟรชเบราว์เซอร์และตรวจสอบการเชื่อมต่อ
- **มอเตอร์หยุดก่อนถึงเป้าหมาย:** ตรวจสอบ current limit, โหลด, ความเร็ว, position error และ Test duration
- **คำสั่งหรือ module ไม่พบ:** ใช้ Python จาก `.venv` ของโปรเจกต์และติดตั้งไลบรารีทั้งสองซ้ำตามขั้นตอน

รายละเอียดเพิ่มเติม: [คู่มือภาษาอังกฤษ](README.md#troubleshooting)

## การทดสอบและ CodeGraph

```sh
python -m unittest discover -s tests -v
```

CodeGraph เป็นเครื่องมือเสริมสำหรับนักพัฒนา ไม่จำเป็นต่อการเปิดแอป หากติดตั้งไว้ ให้สร้าง index หลัง clone ด้วย `codegraph init -i .` และอัปเดตด้วย `codegraph index .` ไฟล์ index, environment และ cache ถูกแยกออกจาก Git ด้วย `.gitignore`

โปรเจกต์นี้เน้นการสาธิตให้ลูกค้าใช้ง่าย ไม่รวมหน้าจอ PID tuning และพารามิเตอร์ขั้นสูงของโปรแกรม engineering

สร้างโดย **Thanabadee Bulunseechart** · **ModuleMore — Be More Creative**
