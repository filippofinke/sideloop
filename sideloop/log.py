import time


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)
