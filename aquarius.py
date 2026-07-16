import sched, time, json, subprocess, os, signal, sys, socket, random, glob
from datetime import datetime

def log(msg):
	line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
	print(line, flush=True)
	try:
		with open("/tmp/aquarius.log", "a") as f:
			f.write(line + "\n")
	except:
		pass

def json_load(uri):
	try:
		with open(uri, encoding="utf-8") as f:
			return json.load(f)
	except Exception as e:
		log(f"Error loading '{uri}': {e}")
		return False

class Aquarius:
	def __init__(self, config):
		self.config = config
		self.children = []
		self.display = config.get("display", ":99")
		self.resolution = config.get("resolution", "1920x1080")
		self.framerate = config.get("framerate", 25)
		self.env = os.environ.copy()
		self.env["DISPLAY"] = self.display
		self.current_source = None
		self.current_url = None
		self.pending_url = None
		self.os1_sock = "/tmp/aquarius-mpv-os1.sock"
		self.player_a_sock = "/tmp/aquarius-mpv-player_a.sock"
		self.player_b_sock = "/tmp/aquarius-mpv-player_b.sock"
		self.os1_proc = None
		self.player_a_proc = None
		self.player_b_proc = None
		self.player_active = "player_a"
		self.chromium_active = "chromium_a"
		self.active_desktop = 0

	def run_bg(self, cmd, **kwargs):
		p = subprocess.Popen(cmd, env=self.env, **kwargs)
		self.children.append(p)
		return p

	def mpv_send(self, sock_path, command):
		try:
			s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			s.settimeout(3)
			s.connect(sock_path)
			s.send(json.dumps({"command": command}).encode() + b"\n")
			time.sleep(0.1)
			resp = b""
			try:
				while True:
					chunk = s.recv(4096)
					if not chunk:
						break
					resp += chunk
					if b"\n" in resp:
						break
			except socket.timeout:
				pass
			s.close()
			first_line = resp.split(b"\n")[0]
			return json.loads(first_line.decode(errors="replace")) if first_line else None
		except Exception as e:
			log(f"mpv IPC ({os.path.basename(sock_path)}): {e}")
			return None

	def mpv_alive(self, name):
		proc = getattr(self, f"{name}_proc")
		sock = getattr(self, f"{name}_sock")
		return proc and proc.poll() is None and os.path.exists(sock)

	def other_desktop(self):
		return 1 - self.active_desktop

	def player_inactive(self):
		return "player_b" if self.player_active == "player_a" else "player_a"

	def chromium_inactive(self):
		return "chromium_b" if self.chromium_active == "chromium_a" else "chromium_a"

	def switch_desktop(self, desktop):
		subprocess.run(["xdotool", "set_desktop", str(desktop)], env=self.env, capture_output=True)
		self.active_desktop = desktop

	def move_window(self, search_args, desktop):
		for i in range(20):
			r = subprocess.run(["xdotool", "search"] + search_args, env=self.env, capture_output=True, text=True)
			wins = r.stdout.split()
			if wins:
				subprocess.run(["xdotool", "set_desktop_for_window", wins[-1], str(desktop)], env=self.env, capture_output=True)
				return True
			time.sleep(0.25)
		log(f"WARNING: window not found for move: {search_args}")
		return False

	def start_xvfb(self):
		log(f"Starting Xvfb on {self.display}")
		subprocess.run(["pkill", "-9", "Xvfb"], capture_output=True)
		time.sleep(0.3)
		self.run_bg(
			["Xvfb", self.display, "-screen", "0", f"{self.resolution}x24", "-ac"],
			stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
		)
		time.sleep(1)
		subprocess.run(["pkill", "-9", "openbox"], capture_output=True)
		rc_path = "/tmp/aquarius-openbox-rc.xml"
		with open(rc_path, "w", encoding="utf-8") as f:
			f.write(
				'<?xml version="1.0" encoding="UTF-8"?>\n'
				'<openbox_config xmlns="http://openbox.org/3.4/rc">\n'
				'  <desktops>\n'
				'    <number>2</number>\n'
				'    <popupTime>0</popupTime>\n'
				'  </desktops>\n'
				'</openbox_config>\n'
			)
		self.run_bg(["openbox", "--config-file", rc_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		time.sleep(1)
		subprocess.run(["wmctrl", "-n", "2"], env=self.env, capture_output=True)
		self.active_desktop = 0
		for cmd in [["xsetroot", "-cursorname", "invisible"], ["unclutter", "-root", "-idle", "0"]]:
			try:
				self.run_bg(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
			except FileNotFoundError:
				pass

	def start_pulse(self):
		log("Setting up PulseAudio")
		subprocess.run(["pactl", "load-module", "module-null-sink", "sink_name=aquarius"], env=self.env, capture_output=True)
		subprocess.run(["pactl", "set-default-sink", "aquarius"], env=self.env, capture_output=True)

	def start_ffmpeg_output(self):
		rtmp_url = self.config.get("rtmp_url", "")
		if not rtmp_url:
			log("ERROR: No rtmp_url")
			return
		log(f"ffmpeg -> {rtmp_url}")
		cmd = (
			f"ffmpeg -hide_banner -loglevel warning -y "
			f"-f x11grab -video_size {self.resolution} -framerate {self.framerate} -i {self.display} "
			f"-f pulse -ar 44100 -ac 2 -i aquarius.monitor "
			f"-c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g {self.framerate * 2} "
			f"-c:a aac -b:a 128k "
			f"-f flv '{rtmp_url}'"
		)
		self.run_bg(["bash", "-c", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

	def start_mpv_instance(self, name, url, sock_path):
		if self.mpv_alive(name):
			log(f"{name} mpv already running")
			return True

		if os.path.exists(sock_path):
			os.remove(sock_path)

		log(f"Starting {name} mpv -> {url}")
		mpv_cmd = (
			f"DISPLAY={self.display} mpv "
			f"--no-terminal "
			f"--title=aquarius-{name} "
			f"--input-ipc-server={sock_path} "
			f"--vo=x11 --hwdec=no "
			f"--vd-lavc-threads=0 "
			f"--framedrop=vo "
			f"--no-sub --no-audio-display "
			f"--geometry={self.resolution}+0+0 --no-border --ontop "
			f"--no-input-default-bindings "
			f"--loop=no "
			f'"{url}" > /tmp/aquarius-mpv-{name}.log 2>&1'
		)

		proc = self.run_bg(["bash", "-c", mpv_cmd])
		setattr(self, f"{name}_proc", proc)

		for i in range(60):
			time.sleep(0.5)
			if os.path.exists(sock_path):
				log(f"{name} mpv ready ({(i+1)*0.5}s)")
				return True

		log(f"{name} mpv FAILED to start")
		try:
			with open(f"/tmp/aquarius-mpv-{name}.log") as f:
				log(f"{name} mpv log: {f.read()[:2000]}")
		except:
			pass
		return False

	def kill_mpv_instance(self, name):
		proc = getattr(self, f"{name}_proc")
		sock_path = getattr(self, f"{name}_sock")
		if proc and proc.poll() is None:
			log(f"Stopping {name} mpv")
			try:
				self.mpv_send(sock_path, ["quit"])
			except:
				pass
			proc.kill()
		pattern = f"input-ipc-server={sock_path}"
		subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True)
		if os.path.exists(sock_path):
			os.remove(sock_path)
		setattr(self, f"{name}_proc", None)

	def kill_all_mpvs(self):
		for name in ["os1", "player_a", "player_b"]:
			self.kill_mpv_instance(name)

	def player_stage(self, url):
		name = self.player_inactive()
		sock = getattr(self, f"{name}_sock")
		if self.mpv_alive(name):
			log(f"{name} loadfile -> {url}")
			self.mpv_send(sock, ["loadfile", url, "replace"])
		else:
			self.start_mpv_instance(name, url, sock)
			time.sleep(1)
		self.mpv_send(sock, ["set_property", "pause", True])
		self.move_window(["--name", f"aquarius-{name}"], self.other_desktop())
		self.current_url = url
		return name

	def player_unpause(self, name):
		sock = getattr(self, f"{name}_sock")
		try:
			self.mpv_send(sock, ["set_property", "pause", False])
		except:
			pass

	def player_pause(self, name):
		sock = getattr(self, f"{name}_sock")
		try:
			self.mpv_send(sock, ["set_property", "pause", True])
		except:
			pass

	def chromium_stage(self, url):
		name = self.chromium_inactive()
		self.kill_chromium(name)
		target = self.other_desktop()

		for binname in ["chromium", "chromium-browser", "google-chrome"]:
			path = subprocess.run(["which", binname], capture_output=True, text=True).stdout.strip()
			if path:
				break
		else:
			path = "chromium"

		target_url = url or "about:blank"
		log(f"Starting Chromium ({name}) -> {target_url}")
		cmd = (
			f"DISPLAY={self.display} {path} "
			f"--ozone-platform=x11 --no-sandbox --disable-gpu "
			f"--kiosk --class=aquarius-{name} "
			f"--user-data-dir=/tmp/aquarius-{name}-profile "
			f"--autoplay-policy=no-user-gesture-required "
			f"--disable-backgrounding-occluded-windows "
			f"--disable-renderer-backgrounding "
			f"--disable-background-timer-throttling "
			f"--disable-features=CalculateNativeWinOcclusion "
			f"--window-size={self.resolution.replace('x', ',')} "
			f"--window-position=0,0 "
			f"'{target_url}' > /tmp/aquarius-{name}.log 2>&1"
		)
		self.run_bg(["bash", "-c", cmd])
		self.move_window(["--class", f"aquarius-{name}"], target)
		time.sleep(5)
		self.current_url = url
		return name

	def kill_chromium(self, name=None):
		if name:
			log(f"Stopping Chromium ({name})")
			subprocess.run(["pkill", "-9", "-f", f"class=aquarius-{name}"], capture_output=True)
		else:
			log("Stopping Chromium")
			subprocess.run(["pkill", "-9", "-f", "chromium.*kiosk"], capture_output=True)

	def cleanup_inactive(self):
		if self.current_source != "os1":
			self.kill_mpv_instance("os1")
		if self.current_source != "chromium":
			self.kill_chromium()
		else:
			self.kill_chromium(self.chromium_inactive())
		if self.current_source != "player":
			for name in ["player_a", "player_b"]:
				if name != self.player_active:
					self.kill_mpv_instance(name)

	def shutdown(self):
		log("SHUTTING DOWN...")
		for proc in self.children:
			try:
				proc.kill()
			except:
				pass
		for p in ["mpv", "chromium", "ffmpeg", "Xvfb", "openbox", "unclutter"]:
			subprocess.run(["pkill", "-9", "-f", p], capture_output=True)
		for f in [self.os1_sock, self.player_a_sock, self.player_b_sock]:
			if os.path.exists(f):
				os.remove(f)
		log("ALL STOPPED")

	def execute(self, command):
		log(str(command))

		if command["command"] == "PROGRAM":
			scene = command["scene"]

			if scene == "OS 1":
				os1_url = self.config.get("os1_rtmp_url", "")
				if os1_url and not self.mpv_alive("os1"):
					self.start_mpv_instance("os1", os1_url, self.os1_sock)
				if self.mpv_alive("os1"):
					target = self.other_desktop()
					self.move_window(["--name", "aquarius-os1"], target)
					self.switch_desktop(target)
					self.current_source = "os1"
				self.cleanup_inactive()

			elif scene == "Media 1":
				name = self.player_inactive()
				if not self.mpv_alive(name):
					url = self.pending_url or self.current_url
					if url:
						name = self.player_stage(url)
					else:
						log("Media 1: no URL loaded")
						name = None
				if name:
					target = self.other_desktop()
					self.switch_desktop(target)
					old_active = self.player_active
					self.player_active = name
					self.player_unpause(name)
					if self.mpv_alive(old_active):
						self.player_pause(old_active)
					self.current_source = "player"
				self.cleanup_inactive()

			elif scene == "Ident":
				ident_folder = self.config.get("ident_folder", "/home/max/idents")
				idents = glob.glob(os.path.join(ident_folder, "*.mp4"))
				if idents:
					name = self.player_stage(random.choice(idents))
					target = self.other_desktop()
					self.switch_desktop(target)
					old_active = self.player_active
					self.player_active = name
					self.player_unpause(name)
					if self.mpv_alive(old_active):
						self.player_pause(old_active)
					self.current_source = "player"
				else:
					log(f"WARNING: No MP4s in {ident_folder}")
				self.cleanup_inactive()

			elif scene == "Clock":
				name = self.chromium_stage(self.config.get("clock_url", "about:blank"))
				target = self.other_desktop()
				self.switch_desktop(target)
				self.chromium_active = name
				self.current_source = "chromium"
				self.cleanup_inactive()

			elif scene == "Breakfiller":
				name = self.chromium_stage(self.config.get("breakfiller_url", "about:blank"))
				target = self.other_desktop()
				self.switch_desktop(target)
				self.chromium_active = name
				self.current_source = "chromium"
				self.cleanup_inactive()

		elif command["command"] == "PREVIEW":
			pass

		elif command["command"] == "LOAD":
			url = command["url"]
			log(f"LOAD: {url}")
			self.pending_url = url
			self.player_stage(url)

		elif command["command"] == "PLAY":
			self.player_unpause(self.player_active)

def main():
	config = json_load("aquarius_config.json")
	if not config:
		print("ERROR: Could not load aquarius_config.json")
		sys.exit(1)

	aquarius = Aquarius(config)

	if "--test" in sys.argv:
		source = None
		if "--source" in sys.argv:
			idx = sys.argv.index("--source")
			source = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

		print(f"TEST MODE - source: {source or 'os1'}")
		log(f"TEST MODE - source: {source or 'os1'}")
		aquarius.start_xvfb()
		aquarius.start_pulse()
		aquarius.start_ffmpeg_output()

		if source == "clock":
			name = aquarius.chromium_stage(aquarius.config.get("clock_url", "about:blank"))
			target = aquarius.other_desktop()
			aquarius.switch_desktop(target)
			aquarius.chromium_active = name
		elif source == "breakfiller":
			name = aquarius.chromium_stage(aquarius.config.get("breakfiller_url", "about:blank"))
			target = aquarius.other_desktop()
			aquarius.switch_desktop(target)
			aquarius.chromium_active = name
		elif source == "ident":
			ident_folder = aquarius.config.get("ident_folder", "/home/max/idents")
			idents = glob.glob(os.path.join(ident_folder, "*.mp4"))
			if idents:
				name = aquarius.player_stage(random.choice(idents))
				target = aquarius.other_desktop()
				aquarius.switch_desktop(target)
				aquarius.player_active = name
				aquarius.player_unpause(name)
			else:
				log(f"WARNING: No MP4s in {ident_folder}")
		elif source == "media":
			if aquarius.pending_url:
				name = aquarius.player_stage(aquarius.pending_url)
				target = aquarius.other_desktop()
				aquarius.switch_desktop(target)
				aquarius.player_active = name
				aquarius.player_unpause(name)
			else:
				log("No media URL. Use: --source media /path/to/file.mp4")
		else:
			os1_url = aquarius.config.get("os1_rtmp_url", "")
			if os1_url:
				aquarius.start_mpv_instance("os1", os1_url, aquarius.os1_sock)
				target = aquarius.other_desktop()
				aquarius.move_window(["--name", "aquarius-os1"], target)
				aquarius.switch_desktop(target)

		log(f"{source or 'os1'} playing - Ctrl+C to stop")
		print(f"{source or 'os1'} playing - Ctrl+C to stop")
		try:
			while True:
				time.sleep(1)
		except KeyboardInterrupt:
			aquarius.shutdown()
		sys.exit(0)

	print("Aquarius starting up...")
	for p in ["mpv", "chromium", "ffmpeg", "Xvfb", "openbox", "unclutter"]:
		subprocess.run(["pkill", "-9", "-f", p], capture_output=True)
	for f in [aquarius.os1_sock, aquarius.player_a_sock, aquarius.player_b_sock, "/tmp/aquarius.log"]:
		if os.path.exists(f):
			os.remove(f)
	time.sleep(1)
	aquarius.start_xvfb()
	aquarius.start_pulse()
	os1_url = aquarius.config.get("os1_rtmp_url", "")
	if os1_url:
		aquarius.start_mpv_instance("os1", os1_url, aquarius.os1_sock)
	aquarius.start_ffmpeg_output()
	time.sleep(2)

	command_list = json_load(config.get("command_output", "command_output.json"))
	if not command_list:
		print("ERROR: Could not load command_output.json")
		sys.exit(1)

	command_sched = sched.scheduler(time.time, time.sleep)
	last_exp = 0
	prev_time = 0

	for index, command in enumerate(command_list):
		if command["time"] != 0 and command["time"] <= time.time():
			last_exp = index

	for index, command in enumerate(command_list):
		if index > last_exp:
			if command["time"] == 0:
				command_sched.enterabs(prev_time, 10, aquarius.execute, argument=(command,))
			else:
				command_sched.enterabs(command["time"], 1, aquarius.execute, argument=(command,))
				prev_time = command["time"]

	log(f"Scheduled {len(command_sched.queue)} commands")

	try:
		while True:
			command_sched.run(blocking=False)
			time.sleep(1)
			if command_sched.queue:
				next_cmd = command_sched.queue[0]
				remaining = next_cmd.time - time.time()
				if remaining > 0:
					print(f"Next: {next_cmd.argument[0]['command']} in {remaining:.0f}s")
	except KeyboardInterrupt:
		aquarius.shutdown()

if __name__ == "__main__":
	main()
