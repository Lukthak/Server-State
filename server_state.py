import threading
import socket
import queue
import time
import os


class _ChatHost:
	"""
	Minimal TCP chat host for local LAN:
	- Listens on 127.0.0.1:<port>
	- Broadcasts plain text lines to all connected clients
	- Pushes received lines into a thread-safe queue for the UI
	"""
	def __init__(self, host='127.0.0.1', port=5678):
		self.host = host
		self.port = int(port)
		self._srv = None
		self._accept_th = None
		self._clients = []
		self._running = False
		self.incoming = queue.Queue()

	def start(self):
		if self._running:
			return
		self._running = True
		try:
			self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
			self._srv.bind((self.host, self.port))
			self._srv.listen(5)
			self._srv.settimeout(0.5)
		except Exception as e:
			print('[ChatHost] Socket setup failed:', e)
			self._running = False
			return

		def _accept_loop():
			while self._running:
				try:
					conn, addr = self._srv.accept()
					conn.settimeout(0.5)
					self._clients.append(conn)
					self.incoming.put(f"[join] {addr[0]}:{addr[1]} conectado")
					t = threading.Thread(target=self._client_loop, args=(conn, addr), daemon=True)
					t.start()
				except socket.timeout:
					continue
				except Exception as e:
					if self._running:
						print('[ChatHost] Accept error:', e)
						time.sleep(0.1)
		self._accept_th = threading.Thread(target=_accept_loop, daemon=True)
		self._accept_th.start()

	def _client_loop(self, conn, addr):
		buf = b''
		try:
			while self._running:
				try:
					data = conn.recv(1024)
					if not data:
						break
					buf += data
					while b'\n' in buf:
						line, buf = buf.split(b'\n', 1)
						try:
							text = line.decode('utf-8', errors='ignore').strip()
						except Exception:
							text = str(line)
						if text:
							self.incoming.put(text)
							print(f"[forward] {text}")
							# Forward to other clients (no echo to the sender)
							try:
								self.broadcast(text, exclude=conn)
							except Exception:
								pass
				except socket.timeout:
					continue
				except Exception:
					break
		finally:
			try:
				conn.close()
			except Exception:
				pass
			self.incoming.put(f"[leave] {addr[0]}:{addr[1]} desconectado")
			try:
				if conn in self._clients:
					self._clients.remove(conn)
			except Exception:
				pass

	def broadcast(self, text: str, exclude=None):
		msg = (text + '\n').encode('utf-8', errors='ignore')
		print(f"[broadcast] to {len(self._clients)} clients: {text}")
		alive = []
		for c in list(self._clients):
			if exclude is not None and c is exclude:
				alive.append(c)
				continue
			try:
				c.sendall(msg)
				alive.append(c)
			except Exception:
				try:
					c.close()
				except Exception:
					pass
		self._clients = alive

	def shutdown(self):
		self._running = False
		try:
			if self._srv:
				self._srv.close()
		except Exception:
			pass
		try:
			for c in self._clients:
				try:
					c.close()
				except Exception:
					pass
			self._clients.clear()
		except Exception:
			pass


class ChatServerState:
	"""
	Pygame state hosting a simple local chat server.
	Flow:
	- Phase 1: Prompt for username
	- Phase 2: Chat UI; Enter to send; ESC to go back
	"""
	def __init__(self, game, host='127.0.0.1', port=5678):
		# Import pygame only when the UI state is actually used,
		# so headless server runs do not require pygame installed.
		import pygame  # local import to avoid hard dependency for server-only
		self.game = game
		self.host = host
		self.port = int(port)
		# Fonts
		self.font_title = pygame.font.SysFont(None, 40, bold=True)
		self.font_text = pygame.font.SysFont(None, 24)
		self.font_small = pygame.font.SysFont(None, 20)
		# UI state
		self.phase = 'username'
		self.username_input = ''
		self.username = ''
		self.chat_input = ''
		self.messages = []  # list[str]
		self.max_messages = 200
		self._caret_timer = 0.0
		# Networking host
		self._host = _ChatHost(host=self.host, port=self.port)
		self._host.start()
		self._push_sys(f"Servidor de chat en {self.host}:{self.port}")
		self._push_sys("Escribe tu nombre y presiona Enter")

	# Utility: push a system message
	def _push_sys(self, text: str):
		ts = time.strftime('%H:%M:%S')
		self.messages.append(f"[{ts}] {text}")
		if len(self.messages) > self.max_messages:
			self.messages = self.messages[-self.max_messages:]

	def handle_event(self, event):
		import pygame  # local import since this path uses pygame
		if event.type == pygame.KEYDOWN:
			if event.key == pygame.K_ESCAPE:
				# Leave and cleanup
				try:
					self._host.shutdown()
				except Exception:
					pass
				from states.menu_state import MenuJugarState
				self.game.manager.change(MenuJugarState(self.game))
				return
			if self.phase == 'username':
				if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
					name = self.username_input.strip()
					if name:
						self.username = name
						self.phase = 'chat'
						self._push_sys(f"Bienvenido, {self.username}. Usa Enter para enviar.")
						# Announce join locally
						self._host.broadcast(f"[join] {self.username}")
					return
				elif event.key == pygame.K_BACKSPACE:
					self.username_input = self.username_input[:-1]
				else:
					ch = getattr(event, 'unicode', '')
					if ch and ch.isprintable():
						self.username_input += ch
			elif self.phase == 'chat':
				if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
					msg = self.chat_input.strip()
					if msg:
						self._send_local(msg)
						self.chat_input = ''
				elif event.key == pygame.K_BACKSPACE:
					self.chat_input = self.chat_input[:-1]
				else:
					ch = getattr(event, 'unicode', '')
					if ch and ch.isprintable():
						self.chat_input += ch

	def _send_local(self, msg: str):
		ts = time.strftime('%H:%M:%S')
		line = f"[{ts}] {self.username}: {msg}"
		self.messages.append(line)
		if len(self.messages) > self.max_messages:
			self.messages = self.messages[-self.max_messages:]
		# Broadcast to connected clients
		try:
			self._host.broadcast(f"{self.username}: {msg}")
		except Exception as e:
			print('[Chat] Broadcast error:', e)

	def update(self, dt):
		# Caret blink
		try:
			self._caret_timer += float(dt)
			if self._caret_timer > 10.0:
				self._caret_timer = 0.0
		except Exception:
			pass
		# Pull incoming messages from network
		try:
			while True:
				try:
					text = self._host.incoming.get_nowait()
				except queue.Empty:
					break
				ts = time.strftime('%H:%M:%S')
				self.messages.append(f"[{ts}] {text}")
				if len(self.messages) > self.max_messages:
					self.messages = self.messages[-self.max_messages:]
		except Exception:
			pass

	def draw(self, screen):
		import pygame  # local import since this path uses pygame
		screen.fill((16, 18, 22))
		w, h = screen.get_size()
		pad = 16
		# Header
		title = self.font_title.render("Chat Local (Host)", False, (220, 230, 240))
		screen.blit(title, (pad, pad))
		info = self.font_small.render(f"{self.host}:{self.port}  |  ESC: volver  ENTER: enviar", False, (160, 170, 180))
		screen.blit(info, (pad, pad + 36))

		if self.phase == 'username':
			prompt = self.font_text.render("Ingresa tu nombre:", False, (240, 240, 240))
			screen.blit(prompt, (pad, pad + 72))
			caret_on = (int(self._caret_timer * 2.0) % 2) == 0
			shown = self.username_input + ("_" if caret_on else "")
			usr = self.font_text.render(shown, False, (210, 220, 230))
			screen.blit(usr, (pad, pad + 104))
			return

		# Messages area
		area_top = pad + 72
		area_bottom = h - (pad + 64)
		line_h = 22
		max_lines = max(1, (area_bottom - area_top) // line_h)
		start = max(0, len(self.messages) - max_lines)
		for i, line in enumerate(self.messages[start:]):
			col = (200, 200, 210)
			if ': ' in line:
				col = (220, 220, 240)
			surf = self.font_small.render(line, False, col)
			screen.blit(surf, (pad, area_top + i * line_h))

		# Input box
		box_y = h - (pad + 40)
		pygame.draw.rect(screen, (34, 38, 44), pygame.Rect(pad - 4, box_y - 6, w - 2 * pad + 8, 30), border_radius=6)
		caret_on = (int(self._caret_timer * 2.0) % 2) == 0
		shown = self.chat_input + ("_" if caret_on else "")
		inp = self.font_text.render(shown, False, (230, 235, 240))
		screen.blit(inp, (pad, box_y))
		start = max(0, len(self.messages) - max_lines)
		for i, line in enumerate(self.messages[start:]):
			col = (200, 200, 210)
			if ': ' in line:
				col = (220, 220, 240)
			surf = self.font_small.render(line, False, col)
			screen.blit(surf, (pad, area_top + i * line_h))

		# Input box
		box_y = h - (pad + 40)
		pygame.draw.rect(screen, (34, 38, 44), pygame.Rect(pad - 4, box_y - 6, w - 2 * pad + 8, 30), border_radius=6)
		caret_on = (int(self._caret_timer * 2.0) % 2) == 0
		shown = self.chat_input + ("_" if caret_on else "")
		inp = self.font_text.render(shown, False, (230, 235, 240))
		screen.blit(inp, (pad, box_y))


def main(host='127.0.0.1', port=5678):
	"""
	Servidor headless: recibe líneas (texto/JSON) y las reenvía
	a todos los demás clientes. No abre el juego.
	"""
	print(f"[Server] Iniciando relay en {host}:{port}")
	host_obj = _ChatHost(host=host, port=port)
	host_obj.start()
	try:
		while True:
			try:
				msg = host_obj.incoming.get(timeout=0.5)
				print(f"[recv] {msg}")
			except queue.Empty:
				pass
	except KeyboardInterrupt:
		print("\n[Server] Apagando...")
	finally:
		host_obj.shutdown()
		print("[Server] Listo")


if __name__ == '__main__':
	import argparse
	parser = argparse.ArgumentParser(description='Servidor headless de relay de chat')
	default_host = os.getenv('HOST', '0.0.0.0')
	default_port = int(os.getenv('PORT', '5678'))
	parser.add_argument('--host', default=default_host, help=f'Dirección de bind (default: {default_host})')
	parser.add_argument('--port', type=int, default=default_port, help=f'Puerto TCP (default: {default_port})')
	args = parser.parse_args()
	main(host=args.host, port=args.port)

