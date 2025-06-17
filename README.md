# bb_monitor

`bb_monitor` is a simple monitoring tool that extracts the first frame from each video recorded by your camera system, adds timestamps and filenames, combines them into a composite image, and sends it to a Telegram bot. It's useful for remote checks on multi-camera setups.

## Features

- Extracts and stamps first frames from recent videos
- Optionally saves individual images
- Creates a vertically stacked composite image
- Sends results via Telegram on a set schedule
- Configurable via Python config files

## Installation

```bash
git clone https://github.com/BioroboticsLab/bb_monitor.git
cd bb_monitor
pip install .
```

## Configuration

You can pass a config file as a command-line argument:

```bash
python bb_monitor.py /path/to/my_config.py
```

If no config is provided, the script will try to load user_config.py, and finally fall back to default_config.py. Create your config by copying and editing default_config.py.

## Running

Run the script using either:

```bash
python bb_monitor.py /path/to/my_config.py
```

or (with user_config.py in the root):
```bash
python bb_monitor.py
```
