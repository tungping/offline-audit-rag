import os
import shutil
import time


def safe_move(src_path, dest_dir, timestamp_func=None):
    if timestamp_func is None:

        def default_timestamp():
            return time.strftime("%Y%m%d_%H%M%S")

        timestamp_func = default_timestamp

    filename = os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, filename)

    if os.path.exists(dest_path):
        base, ext = os.path.splitext(filename)
        timestamp = timestamp_func()
        dest_path = os.path.join(dest_dir, f"{base}_{timestamp}{ext}")
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{base}_{timestamp}_{counter}{ext}")
            counter += 1

    shutil.move(src_path, dest_path)
    return dest_path


def unique_file_path(file_path):
    if not os.path.exists(file_path):
        return file_path

    base, ext = os.path.splitext(file_path)
    counter = 1
    candidate = f"{base}_{counter}{ext}"
    while os.path.exists(candidate):
        counter += 1
        candidate = f"{base}_{counter}{ext}"
    return candidate
