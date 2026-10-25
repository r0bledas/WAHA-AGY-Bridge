---
name: urbani-qr
description: Generate a Metrorrey QR ticket image from C:\Users\T450\Documents\UrbaniRE\workbench_live.html and send it to WhatsApp.
---
1. Use workbench_live.html (the online, non server-dependent version).
2. Station: use the user's choice from selStationPreset; default is "L2 Universidad (Original)" (25.7264403,-100.2450212).
3. Render with headless Chrome (C:\Program Files\Google\Chrome\Application\chrome.exe, --headless=new --no-sandbox --virtual-time-budget=8000). Never host the file on a server.
4. Crop to the QR box only (element .qr-container-box) and save as PNG.
5. Send with python C:\waha\send_file.py "<png>" "<station, fare, expiry time>".
6. In the reply, list all stations from selStationPreset and state which one was used.
7. Fresh code every request: load the page anew in headless Chrome, never reuse an old image. Check that the "Actualizado" time on the page differs from the previous send.
8. Crop recipe that worked: window 500x1000, then crop box (100,218,386,506) with PIL. View the crop before sending to confirm it holds only the QR.
9. Caption: station, fare, generation time, expiry (15 min).
10. The user often has no mobile data: send the image itself over WhatsApp, never links or hosted pages.
11. Delete temp files afterwards (urbani_full.png, urbani_qr.png, tmp_chrome folders).
12. Non-default stations: if the user names one, select it in selStationPreset before rendering (method not yet tested; test it and record it here).
