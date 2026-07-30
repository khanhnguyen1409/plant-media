import os
import re
import random
import shutil
import time
import subprocess
import tempfile
import json
from datetime import datetime
import pytz

import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageOps
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
import io

# ---------------------------------------------------------
# 1. CẤU HÌNH GIAO DIỆN PLANTMEDIA
# ---------------------------------------------------------
st.set_page_config(page_title="PlantMedia - Lá Hát Garden", page_icon="🌿", layout="centered")

st.title("🌿 PlantMedia")
st.caption("Ứng dụng tự động xử lý Ảnh & Video cây cảnh cho Lá Hát Garden")

SPREADSHEET_ID = "1UN2RpqIBex1ljpxYmcVNBwTBM7YOvU-XqYFXSgGaHBs"
TARGET_GID = 1465782467

if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = getattr(Image.Resampling, 'LANCZOS', Image.LANCZOS)

# ---------------------------------------------------------
# 2. KẾT NỐI GOOGLE APIS TỪ STREAMLIT SECRETS
# ---------------------------------------------------------
@st.cache_resource
def get_google_services():
    try:
        if "gcp_json" in st.secrets:
            creds_json = json.loads(st.secrets["gcp_json"], strict=False)
        elif "gcp_service_account" in st.secrets:
            creds_json = dict(st.secrets["gcp_service_account"])
            if "private_key" in creds_json:
                creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        else:
            st.error("❌ Chưa tìm thấy cấu hình Secrets trong Streamlit!")
            return None, None

        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = service_account.Credentials.from_service_account_info(creds_json, scopes=scopes)
        sheets_service = build('sheets', 'v4', credentials=creds)
        drive_service = build('drive', 'v3', credentials=creds)
        return sheets_service, drive_service
    except Exception as e:
        st.error(f"❌ Lỗi kết nối Google Service Account: {e}")
        return None, None

sheets_service, drive_service = get_google_services()

# ---------------------------------------------------------
# 3. CÁC HÀM XỬ LÝ GOOGLE DRIVE FILE/FOLDER (HỖ TRỢ ALL DRIVES)
# ---------------------------------------------------------
def find_folder_id(folder_name, parent_id=None):
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = drive_service.files().list(
        q=query, 
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def list_files_in_folder(folder_id):
    query = f"'{folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
    results = drive_service.files().list(
        q=query, 
        fields="files(id, name, mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    return results.get('files', [])

def download_file_from_drive(file_id, local_path):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

def upload_file_to_drive(local_path, drive_folder_id, file_name, mime_type='image/jpeg'):
    if not drive_folder_id:
        st.error(f"❌ Lỗi: Thư mục lưu trữ đích cho file `{file_name}` không tồn tại trên Google Drive!")
        return None

    file_metadata = {'name': file_name, 'parents': [drive_folder_id]}
    try:
        media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
        file = drive_service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id',
            supportsAllDrives=True
        ).execute(num_retries=3)
        return file.get('id')
    except HttpError as e:
        err_msg = e.content.decode('utf-8') if hasattr(e, 'content') else str(e)
        st.error(f"❌ Lỗi Google Drive API ({e.resp.status}) khi upload '{file_name}': {err_msg}")
        raise e

def move_drive_file(file_id, old_folder_id, new_folder_id):
    drive_service.files().update(
        fileId=file_id,
        addParents=new_folder_id,
        removeParents=old_folder_id,
        fields='id, parents',
        supportsAllDrives=True
    ).execute()

# ---------------------------------------------------------
# 4. HÀM XỬ LÝ MEDIA (IMAGE & VIDEO)
# ---------------------------------------------------------
def process_image(local_img_path, logo_path, font_path, display_code, price, skip_price=False):
    img = ImageOps.exif_transpose(Image.open(local_img_path)).convert("RGBA")
    w, h = img.size
    
    logo = Image.open(logo_path).convert("RGBA")
    logo_w = int(w * 0.12)
    logo_resized = logo.resize((logo_w, int(logo.size[1] * (logo_w / logo.size[0]))), Image.Resampling.LANCZOS)
    
    img_no_price = img.copy()
    img_no_price.paste(logo_resized, (w - logo_w - int(w*0.01), int(h*0.01)), logo_resized)
    
    draw = ImageDraw.Draw(img_no_price)
    try: font = ImageFont.truetype(font_path, int(h*0.013))
    except: font = ImageFont.load_default()
    
    bbox = draw.textbbox((0,0), display_code, font=font)
    draw.text(((w - (bbox[2]-bbox[0]))//2, h - (bbox[3]-bbox[1]) - int(h*0.025)), display_code, fill="white", font=font)
    
    out_no_price = tempfile.mktemp(suffix=".jpg")
    img_no_price.convert("RGB").save(out_no_price, "JPEG", quality=95)
    
    out_price = None
    if price and not skip_price:
        img_price = img_no_price.copy()
        try: tag_font = ImageFont.truetype(font_path, int(h*0.025))
        except: tag_font = ImageFont.load_default()
        
        price_text = f"{price}K"
        tag_bbox = draw.textbbox((0,0), price_text, font=tag_font)
        tag_w, tag_h = (tag_bbox[2]-tag_bbox[0]) + 40, (tag_bbox[3]-tag_bbox[1]) + 20
        tag = Image.new("RGBA", (tag_w, tag_h), (0,0,0,0))
        tag_draw = ImageDraw.Draw(tag)
        tag_draw.rounded_rectangle([0, 0, tag_w, tag_h], radius=15, fill=(46, 125, 50, 255))
        tag_draw.text((20, 10 - tag_bbox[1]), price_text, fill="white", font=tag_font)
        
        img_price.paste(tag, (int(w*0.01), int(h*0.01) + (logo_resized.size[1]//2) - (tag.size[1]//2)), tag)
        out_price = tempfile.mktemp(suffix=".jpg")
        img_price.convert("RGB").save(out_price, "JPEG", quality=95)
        
    return out_no_price, out_price

def process_video(local_vid_path, logo_path, font_path, music_path, display_code):
    from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip, AudioFileClip, CompositeAudioClip
    import moviepy.audio.fx.all as afx
    from moviepy.audio.fx.all import audio_loop

    fixed_vid = tempfile.mktemp(suffix=".mp4")
    subprocess.run([
        "ffmpeg", "-y", "-i", local_vid_path,
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-metadata:s:v:0", "rotate=0",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        fixed_vid
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    video = VideoFileClip(fixed_vid if os.path.exists(fixed_vid) else local_vid_path)
    dur = video.duration
    vw, vh = video.size

    if music_path:
        v_aud = video.audio
        bg_raw = AudioFileClip(music_path)
        bg_aud = audio_loop(bg_raw, duration=dur) if bg_raw.duration < dur else bg_raw.set_duration(dur)
        bg_aud = bg_aud.fx(afx.audio_fadein, 1.0).fx(afx.audio_fadeout, 1.0).volumex(0.8)
        final_aud = CompositeAudioClip([v_aud.volumex(1.2), bg_aud]) if v_aud else bg_aud
    else:
        final_aud = video.audio

    logo_w = int(vw * 0.12)
    logo = ImageClip(logo_path).resize(width=logo_w).set_duration(dur).set_position((vw - logo_w - int(vw*0.01), int(vh*0.01)))

    font_size = int(vh * 0.025)
    try: font = ImageFont.truetype(font_path, font_size)
    except: font = ImageFont.load_default()

    dummy_img = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy_img)
    bbox = draw.textbbox((0, 0), display_code, font=font)
    txt_w = (bbox[2] - bbox[0]) + 40
    txt_h = (bbox[3] - bbox[1]) + 20

    txt_img = Image.new("RGBA", (txt_w, txt_h), (0, 0, 0, 0))
    txt_draw = ImageDraw.Draw(txt_img)
    txt_draw.text((20, 10 - bbox[1]), display_code, fill="white", font=font)

    temp_txt_path = tempfile.mktemp(suffix=".png")
    txt_img.save(temp_txt_path, "PNG")

    name = ImageClip(temp_txt_path).set_duration(dur).set_position(("center", vh - int(vh*0.06)))

    combined = CompositeVideoClip([video.without_audio(), logo, name], size=(vw, vh))
    if final_aud: combined.audio = final_aud

    out_vid_path = tempfile.mktemp(suffix=".mp4")
    combined.write_videofile(
        out_vid_path, 
        codec="libx264", 
        audio_codec="aac", 
        preset="ultrafast", 
        bitrate="2500k", 
        threads=2, 
        verbose=False, 
        logger=None
    )
    combined.close(); video.close()
    return out_vid_path

# ---------------------------------------------------------
# 5. GIAO DIỆN CHÍNH STREAMLIT
# ---------------------------------------------------------
if not sheets_service or not drive_service:
    st.stop()

st.info("📌 Hệ thống PlantMedia đã sẵn sàng!")

if st.button("🚀 BẮT ĐẦU XỬ LÝ MEDIA (RUN)", type="primary", use_container_width=True):
    with st.spinner("Đang kiểm tra cấu trúc thư mục Google Drive..."):
        root_id = find_folder_id("Plant_Sales")
        if not root_id:
            st.error("❌ Không tìm thấy thư mục 'Plant_Sales' trên Drive. Vui lòng kiểm tra lại tên thư mục!")
            st.stop()
            
        raw_id = find_folder_id("01_Raw", root_id)
        orig_id = find_folder_id("00_Original", raw_id) if raw_id else None
        assets_id = find_folder_id("02_Assets", root_id)
        edited_id = find_folder_id("03_Edited", root_id)
        music_id = find_folder_id("Music", assets_id) if assets_id else None

        # Kiểm tra kĩ tính sẵn sàng của các thư mục bắt buộc
        if not raw_id or not edited_id:
            st.error(f"❌ Thiếu thư mục trên Drive! (Thư mục '01_Raw': {'✅ OK' if raw_id else '❌ Không tìm thấy'}, Thư mục '03_Edited': {'✅ OK' if edited_id else '❌ Không tìm thấy'})")
            st.stop()

    tab_name = "Plant_Sales"
    rows = sheets_service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=f"'{tab_name}'!A1:E10000").execute().get('values', [])
    headers = [str(h).strip().lower() for h in rows[0]]
    idx_ms = headers.index('ma_so') if 'ma_so' in headers else 0
    idx_gia = headers.index('gia_ban') if 'gia_ban' in headers else 1
    idx_anh = headers.index('anh') if 'anh' in headers else 2
    idx_vid = headers.index('video') if 'video' in headers else 3
    idx_run = headers.index('run_again') if 'run_again' in headers else 4

    db = {}
    for r_idx, row in enumerate(rows[1:], start=2):
        if len(row) <= idx_ms or not row[idx_ms]: continue
        ms = str(row[idx_ms]).strip()
        db[ms] = {
            'row_num': r_idx,
            'Gia_Ban': str(row[idx_gia]).strip() if len(row) > idx_gia else '',
            'Run_Again': (str(row[idx_run]).strip().upper() == 'TRUE') if len(row) > idx_run else False,
        }

    assets_files = list_files_in_folder(assets_id) if assets_id else []
    logo_file = next((f for f in assets_files if f['name'].lower() == 'logo.png'), None)
    font_file = next((f for f in assets_files if f['name'].lower() == 'font.ttf'), None)

    temp_logo = tempfile.mktemp(suffix=".png")
    temp_font = tempfile.mktemp(suffix=".ttf")
    if logo_file: download_file_from_drive(logo_file['id'], temp_logo)
    if font_file: download_file_from_drive(font_file['id'], temp_font)

    music_files = list_files_in_folder(music_id) if music_id else []

    raw_files = list_files_in_folder(raw_id)
    if not raw_files:
        st.warning("⚠️ Không có file nào trong thư mục `01_Raw`!")
        st.stop()

    st.write(f"📁 Tìm thấy **{len(raw_files)}** file trong thư mục `01_Raw`.")

    progress_bar = st.progress(0)
    status_log = st.empty()

    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    time_now = datetime.now(tz_vn).strftime("%H:%M, %d/%m/%Y")
    status_text = f"{time_now} (PlantMedia)"

    count_img, count_vid = 0, 0

    for idx, f in enumerate(raw_files):
        fname = f['name']
        n_only, ext = os.path.splitext(fname)
        ext_low = ext.lower()

        match = re.fullmatch(r"(\d{1,4})(?:\s*)([LR])?(?:\s*\(\d+\))?", n_only.strip(), re.IGNORECASE)
        if not match: continue

        ma_so = match.group(1)
        sub_type = (match.group(2) or "").upper()
        disp_raw = f"{ma_so}{sub_type}"

        if ma_so not in db: continue
        r_info = db[ma_so]

        local_raw = tempfile.mktemp(suffix=ext_low)
        download_file_from_drive(f['id'], local_raw)

        if ext_low in ['.jpg', '.jpeg', '.png', '.heic']:
            status_log.text(f"📸 Đang xử lý ảnh: {disp_raw}...")
            out_np, out_p = process_image(local_raw, temp_logo, temp_font, disp_raw, r_info['Gia_Ban'], skip_price=(sub_type in ['L','R']))
            
            upload_file_to_drive(out_np, edited_id, f"{disp_raw}_.jpg")
            if out_p:
                upload_file_to_drive(out_p, edited_id, f"{disp_raw}_{r_info['Gia_Ban']}k.jpg")

            if orig_id:
                move_drive_file(f['id'], raw_id, orig_id)
            count_img += 1

        elif ext_low in ['.mp4', '.mov', '.avi', '.hevc']:
            status_log.text(f"🎬 Đang render video: {disp_raw}...")
            local_music = None
            if music_files:
                rand_m = random.choice(music_files)
                local_music = tempfile.mktemp(suffix=".mp3")
                download_file_from_drive(rand_m['id'], local_music)

            out_vid = process_video(local_raw, temp_logo, temp_font, local_music, disp_raw)
            upload_file_to_drive(out_vid, edited_id, f"{disp_raw}_video.mp4", mime_type='video/mp4')
            if orig_id:
                move_drive_file(f['id'], raw_id, orig_id)
            count_vid += 1

        progress_bar.progress((idx + 1) / len(raw_files))

    st.success(f"🎉 HOÀN TẤT! Đã tạo **{count_img}** ảnh và **{count_vid}** video lên Drive.")
