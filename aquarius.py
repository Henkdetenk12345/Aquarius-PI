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
		self.media_sock = "/tmp/aquarius-mpv-media.sock"
		self.ident_sock = "/tmp/aquarius-mpv-ident.sock"
		self.os1_proc = None
		self.media_proc = None
		self.ident_proc = None

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
		self.run_bg(["openbox"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		time.sleep(1)
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
			f"--input-ipc-server={sock_path} "
			f"--vo=x11 --hwdec=no "
			f"--no-sub --no-audio-display "
			f"--fullscreen --fs "
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
		for name in ["os1", "media", "ident"]:
			self.kill_mpv_instance(name)

	def start_chromium(self, url=None, kill_mpvs=True):
		if self.current_source == "chromium" and self.current_url == url:
			return

		if kill_mpvs:
			self.kill_all_mpvs()

		if self.current_source == "chromium":
			log(f"Navigate chromium -> {url}")
			subprocess.run(
				["xdotool", "search", "--name", "chromium", "key", "F5"],
				env=self.env, capture_output=True
			)
			self.current_url = url
			return

		for name in ["chromium", "chromium-browser", "google-chrome"]:
			path = subprocess.run(["which", name], capture_output=True, text=True).stdout.strip()
			if path:
				break
		else:
			path = "chromium"

		target = url or "about:blank"
		log(f"Starting Chromium -> {target}")
		cmd = (
			f"DISPLAY={self.display} {path} "
			f"--kiosk --noerrdialogs --disable-infobars "
			f"--disable-session-crashed-bubble --disable-restore-session-state "
			f"--no-first-run "
			f"--enable-gpu-rasterization --ignore-gpu-blocklist "
			f"--hide-scrollbars --autoplay-policy=no-user-gesture-required "
			f"--disable-features=ScrollbarUI "
			f"--window-size={self.resolution.replace('x', ',')} "
			f"--window-position=0,0 --no-sandbox "
			f"'{target}' > /tmp/aquarius-chromium.log 2>&1"
		)
		self.run_bg(["bash", "-c", cmd])
		self.current_source = "chromium"
		self.current_url = url
		time.sleep(2)

	def kill_chromium(self):
		log("Stopping Chromium")
		subprocess.run(["pkill", "-9", "-f", "chromium.*kiosk"], capture_output=True)
		subprocess.run(["pkill", "-9", "-f", "chromium.*no-sandbox"], capture_output=True)
		time.sleep(0.5)
		if self.current_source == "chromium":
			self.current_source = None
			self.current_url = None

	def shutdown(self):
		log("SHUTTING DOWN...")
		for proc in self.children:
			try:
				proc.kill()
			except:
				pass
		for p in ["mpv", "chromium", "ffmpeg", "Xvfb", "openbox", "unclutter"]:
			subprocess.run(["pkill", "-9", "-f", p], capture_output=True)
		for f in [self.os1_sock, self.media_sock, self.ident_sock]:
			if os.path.exists(f):
				os.remove(f)
		log("ALL STOPPED")

	def execute(self, command):
		log(str(command))

		if command["command"] == "PROGRAM":
			scene = command["scene"]

			if scene == "OS 1":
				self.kill_chromium()
				self.kill_mpv_instance("ident")
				self.kill_mpv_instance("media")
				os1_url = self.config.get("os1_rtmp_url", "")
				if os1_url:
					self.start_mpv_instance("os1", os1_url, self.os1_sock)
					self.current_source = "os1"

			elif scene == "Media 1":
				self.kill_chromium()
				self.kill_mpv_instance("ident")
				url = self.pending_url or self.current_url
				if url:
					if not self.mpv_alive("media"):
						self.start_mpv_instance("media", url, self.media_sock)
					self.current_source = "media"
					self.current_url = url
				else:
					log("Media 1: no URL loaded")

			elif scene == "Ident":
				self.kill_chromium()
				self.kill_mpv_instance("os1")
				ident_folder = self.config.get("ident_folder", "/home/max/idents")
				idents = glob.glob(os.path.join(ident_folder, "*.mp4"))
				if idents:
					self.start_mpv_instance("ident", random.choice(idents), self.ident_sock)
				else:
					log(f"WARNING: No MP4s in {ident_folder}")

			elif scene == "Clock":
				self.kill_mpv_instance("ident")
				self.kill_mpv_instance("os1")
				self.start_chromium(self.config.get("clock_url", "about:blank"), kill_mpvs=False)

			elif scene == "Breakfiller":
				self.kill_mpv_instance("ident")
				self.kill_mpv_instance("os1")
				self.start_chromium(self.config.get("breakfiller_url", "about:blank"), kill_mpvs=False)

		elif command["command"] == "PREVIEW":
			pass

		elif command["command"] == "LOAD":
			url = command["url"]
			log(f"LOAD: {url}")
			self.pending_url = url

		elif command["command"] == "PLAY":
			if self.mpv_alive("media"):
				try:
					self.mpv_send(self.media_sock, ["set_property", "pause", False])
				except:
					pass

def main():
	config = json_load("aquarius_config.json")
	if not config:
		print("ERROR: Could not load aquarius_config.json")
		sys.exit(1)

	aquarius = Aquarius(config)

	if "--test" in sys.argv:
		print("TEST MODE")
		log("TEST MODE")
		aquarius.start_xvfb()
		aquarius.start_pulse()
		aquarius.start_ffmpeg_output()
		os1_url = aquarius.config.get("os1_rtmp_url", "")
		if os1_url:
			aquarius.start_mpv_instance("os1", os1_url, aquarius.os1_sock)
		log("OS1 playing - Ctrl+C to stop")
		print("OS1 playing - Ctrl+C to stop")
		try:
			while True:
				time.sleep(1)
		except KeyboardInterrupt:
			aquarius.shutdown()
		sys.exit(0)

	print("Aquarius starting up...")
	for p in ["mpv", "chromium", "ffmpeg", "Xvfb", "openbox", "unclutter"]:
		subprocess.run(["pkill", "-9", "-f", p], capture_output=True)
	for f in [aquarius.os1_sock, aquarius.media_sock, aquarius.ident_sock, "/tmp/aquarius.log"]:
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
