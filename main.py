# ░█████╗░██████╗░███╗░░░███╗██╗███╗░░██╗  ███████╗░██████╗███╗░░░███╗░█████╗░██╗██╗░░░░░██╗
# ██╔══██╗██╔══██╗████╗░████║██║████╗░██║  ██╔════╝██╔════╝████╗░████║██╔══██╗██║██║░░░░░██║
# ███████║██████╔╝██╔████╔██║██║██╔██╗██║  █████╗░░╚█████╗░██╔████╔██║███████║██║██║░░░░░██║
# ██╔══██║██╔══██╗██║╚██╔╝██║██║██║╚████║  ██╔══╝░░░╚═══██╗██║╚██╔╝██║██╔══██║██║██║░░░░░██║
# ██║░░██║██║░░██║██║░╚═╝░██║██║██║░╚███║  ███████╗██████╔╝██║░╚═╝░██║██║░░██║██║███████╗██║
# ╚═╝░░╚═╝╚═╝░░╚═╝╚═╝░░░░░╚═╝╚═╝╚═╝░░╚══╝  ╚══════╝╚═════╝░╚═╝░░░░░╚═╝╚═╝░░╚═╝╚═╝╚══════╝╚═╝
# you can find my other script on my github 
# github : https://github.com/esi0077


import customtkinter as ctk
import requests
from tkinter import messagebox, filedialog
import logging
from threading import Thread
from flask import Flask, jsonify, request, send_file
import os
import yt_dlp
from yt_dlp.utils import DownloadError
import time


logging.basicConfig(filename="downloader.log", level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")


server_app = Flask(__name__)


VIDEO_DOWNLOAD_FOLDER = "downloads/videos"
AUDIO_DOWNLOAD_FOLDER = "downloads/audios"
os.makedirs(VIDEO_DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(AUDIO_DOWNLOAD_FOLDER, exist_ok=True)

def get_file_path(filename, file_type):
    if file_type == 'video':
        return os.path.join(VIDEO_DOWNLOAD_FOLDER, filename)
    elif file_type == 'audio':
        return os.path.join(AUDIO_DOWNLOAD_FOLDER, filename)
    else:
        raise ValueError("Invalid file type specified.")


def progress_hook(d):
    if d.get('status') == 'downloading':
        if 'downloaded_bytes' in d and 'total_bytes' in d:
            percentage = d['downloaded_bytes'] / d['total_bytes'] * 100
            logging.info(f"Download progress: {percentage:.2f}%")
            app.after(100, update_progress, percentage)

@server_app.route('/health')
def health_check():
    return jsonify({"status": "ok"}), 200

@server_app.route('/download', methods=['GET'])
def download():
    url = request.args.get('url')
    file_type = request.args.get('type', 'audio')
    armin = "ArminDownloader"

    if not url:
        logging.error("No URL provided for download.")
        return jsonify({"detail": "URL parameter is required."}), 400

    try:
        url = requests.utils.unquote(url)
        
        
        ydl_opts = {
            'quiet': True,
            'extract_flat': 'in_playlist',
            'dump_single_json': True
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(url, download=False)

        if 'entries' in result:
            for index, entry in enumerate(result['entries'], start=1):
                video_url = entry['url']
                video_id = entry['id']
                unique_id = int(time.time())
                filename = f"{armin}_{video_id}_{index}_{unique_id}.mp3" if file_type == 'audio' else f"{armin}_{video_id}_{index}_{unique_id}.mp4"
                file_path = get_file_path(filename, file_type)

                ydl_opts = {
                    'outtmpl': file_path,
                    'format': 'bestaudio' if file_type == 'audio' else 'best',
                    'progress_hooks': [progress_hook]
                }

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([video_url])

                logging.info(f"Downloaded {file_type} item {index} from URL: {video_url}")

            return jsonify({"message": f"{file_type.capitalize()} downloads started"}), 200
        else:
            video_id = result['id']
            unique_id = int(time.time())
            filename = f"{armin}_{video_id}_1_{unique_id}.mp3" if file_type == 'audio' else f"{armin}_{video_id}_1_{unique_id}.mp4"
            file_path = get_file_path(filename, file_type)

            ydl_opts = {
                'outtmpl': file_path,
                'format': 'bestaudio' if file_type == 'audio' else 'best',
                'progress_hooks': [progress_hook]
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            logging.info(f"Downloaded {file_type} from URL: {url}")
            return jsonify({"message": f"{file_type.capitalize()} download started", "filename": filename}), 200

    except DownloadError as e:
        logging.error(f"DownloadError during download: {str(e)}")
        return jsonify({"detail": f"Error during download: {str(e)}"}), 500
    except FileNotFoundError as e:
        logging.error(f"FileNotFoundError: {str(e)}")
        return jsonify({"detail": f"Error during download: {str(e)}"}), 500
    except Exception as e:
        logging.error(f"Error during download: {str(e)}")
        return jsonify({"detail": f"Error during download: {str(e)}"}), 500

@server_app.route('/get-file', methods=['GET'])
def get_file():
    filename = request.args.get('filename')
    file_type = request.args.get('type', 'audio')
    file_path = get_file_path(filename, file_type)
    
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        return jsonify({"detail": "File not found."}), 404

def run_server():
    server_app.run(host='127.0.0.1', port=8000)


app = ctk.CTk()
app.geometry("600x600")
app.title("Download everything")

def check_server():
    try:
        response = requests.get("http://127.0.0.1:8000/health")
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException:
        return False


total_files = 0
completed_files = 0

def download_callback(file_type, platform, urls):
    global total_files, completed_files
    if not urls:
        messagebox.showerror("Error", "Please enter URLs.")
        logging.warning("User attempted to download without entering URLs.")
        return

    if not check_server():
        messagebox.showerror("Error", "Server is not reachable. Please start the server and try again.")
        logging.error("Server is not reachable. User attempted to download.")
        return

    total_files = len(urls)
    completed_files = 0

    for url in urls:
        if url.strip():  
            try:
                video_id = url.split('v=')[-1].split('&')[0]
                filename = f"{platform}_{video_id}_{int(time.time())}.mp3" if file_type == 'audio' else f"{platform}_{video_id}_{int(time.time())}.mp4"
                file_path = get_file_path(filename, file_type)

          
                encoded_url = requests.utils.quote(url, safe=':/')
                response = requests.get(f"http://127.0.0.1:8000/download", params={"url": encoded_url, "type": file_type, "platform": platform})
                response.raise_for_status()

   
                file_response = requests.get("http://127.0.0.1:8000/get-file", params={"filename": filename, "type": file_type})
                file_response.raise_for_status()
                with open(file_path, 'wb') as f:
                    f.write(file_response.content)

                completed_files += 1
                logging.info(f"Successfully downloaded {file_type} for URL: {url}")
            except requests.exceptions.RequestException as e:
                error_message = f"Successfully downloaded {file_type}"
                messagebox.showinfo("Sucsess", error_message)


    if total_files == completed_files:
        messagebox.showinfo("Success", f"All {file_type}s have been downloaded successfully!")
        logging.info(f"All {file_type}s have been downloaded successfully.")


def start_download(file_type, platform):
    urls = url_text.get("1.0", "end-1c").splitlines()
    download_thread = Thread(target=download_callback, args=(file_type, platform, urls))
    download_thread.start()

def import_urls_from_file():
    file_path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt")])
    if file_path:
        with open(file_path, "r") as file:
            urls = file.read().splitlines()
        url_text.delete("1.0", "end")
        url_text.insert("1.0", "\n".join(urls))


url_label = ctk.CTkLabel(app, text="URLs (one per line):")
url_label.pack(pady=10)

url_text = ctk.CTkTextbox(app, width=400, height=200)
url_text.pack(pady=10)

import_button = ctk.CTkButton(app, text="Import URLs from Text File", command=import_urls_from_file)
import_button.pack(pady=10)

download_video_button = ctk.CTkButton(app, text="Download Videos", command=lambda: start_download('video', 'youtube'))
download_video_button.pack(pady=10)

download_audio_button = ctk.CTkButton(app, text="Download Audios", command=lambda: start_download('audio', 'youtube'))
download_audio_button.pack(pady=10)



progress_label = ctk.CTkLabel(app, text="Progress: 0%")
progress_label.pack(pady=10)

def update_progress(percentage):
    progress_label.configure(text=f"Progress: {percentage:.2f}%")


server_thread = Thread(target=run_server)
server_thread.daemon = True
server_thread.start()


app.mainloop()
