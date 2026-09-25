import asyncio
import json
import random
import sys

HOST = "balatro.virtualized.dev"
PORT = 8788
MOD_VERSION = "0.5.5"

# Realistic PvP score curves per Ante
PVP_TARGETS = {
    1: 3500,
    2: 15000,
    3: 50000,
    4: 180000,
    5: 600000,
    6: 2500000,
    7: 10000000,
    8: 40000000,
}

class BalatroSyncBot:
    def __init__(self, name="PracticeBot", color_id=14):
        self.username = f"{name}~{color_id}"
        self.reader = None
        self.writer = None
        self.is_running = True
        self.lobby_code = None
        
        # State tracking
        self.game_active = False
        self.current_ante = 1
        self.current_blind = "bl_small"
        self.is_in_pvp = False
        self.pvp_task = None
        self.is_lobby_ready = False

    async def send(self, payload: dict):
        raw = json.dumps(payload) + "\n"
        print(f"[SEND] -> {raw.strip()}", flush=True)
        self.writer.write(raw.encode("utf-8"))
        await self.writer.drain()

    async def connect(self, lobby_code):
        self.lobby_code = lobby_code.upper()
        print(f"[*] Connecting to {HOST}:{PORT}...", flush=True)
        self.reader, self.writer = await asyncio.open_connection(HOST, PORT)
        print("[+] Connected over raw TCP!\n", flush=True)
        
        await asyncio.gather(
            self.listen_loop(),
            self.interactive_cli()
        )

    async def listen_loop(self):
        buffer = ""
        while self.is_running:
            chunk = await self.reader.read(4096)
            if not chunk:
                print("[-] Server closed connection.", flush=True)
                self.is_running = False
                break

            buffer += chunk.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    await self.handle_message(msg)
                except json.JSONDecodeError:
                    pass

    async def handle_message(self, msg: dict):
        action = msg.get("action")

        # 1. KeepAlive Heartbeat
        if action == "keepAlive":
            await self.send({"action": "keepAliveAck"})
            return

        print(f"[RECV] <- {msg}", flush=True)

        # 2. Handshake
        if action == "version":
            await self.send({"action": "version", "version": MOD_VERSION})
            await self.send({
                "action": "username",
                "username": self.username,
                "modHash": "00000000000000000000"
            })
            await self.send({"action": "joinLobby", "code": self.lobby_code})

        # 3. Lobby Joined
        elif action == "joinedLobby":
            print(f"[✓] Joined Lobby {msg.get('code')}. Readying up...", flush=True)
            await self.send({"action": "syncClient", "isCached": False})
            await self.send({"action": "lobbyInfo"})
            await asyncio.sleep(0.5)
            await self.send_ready_lobby()

        # 4. Lobby Info Updates (Detects if bot needs to ready up in lobby)
        elif action == "lobbyInfo":
            if not self.game_active:
                guest_ready = msg.get("guestReady", False)
                if not guest_ready:
                    print("[*] Lobby updated: Bot is unready. Sending readyLobby...", flush=True)
                    await asyncio.sleep(0.5)
                    await self.send_ready_lobby()

        # 5. Game Started
        elif action == "startGame":
            print("\n[🚀] GAME STARTED! Ante 1 active.", flush=True)
            self.game_active = True
            self.is_lobby_ready = False
            self.current_ante = 1
            self.current_blind = "bl_small"
            self.is_in_pvp = False
            await self.send({"action": "setAnte", "ante": 1})
            await self.send({"action": "setFurthestBlind", "furthestBlind": "bl_small"})
            await self.send({"action": "setLocation", "location": "loc_selecting-bl_small"})

        # 6. Track Player Location
        elif action == "enemyLocation":
            loc = msg.get("location", "")
            await self.sync_location(loc)

        # 7. PvP Blind Start (Countdown finished)
        elif action == "startBlind":
            print("\n[⚔️] PvP BOSS BLIND TRIGGERED! Playing hands...", flush=True)
            self.is_in_pvp = True
            if self.pvp_task and not self.pvp_task.done():
                self.pvp_task.cancel()
            self.pvp_task = asyncio.create_task(self.play_pvp_hands())

        # 8. PvP Blind End (Server resolves winner)
        elif action == "endPvP":
            print("\n[✓] PvP ROUND RESOLVED! Moving to next Ante...", flush=True)
            self.is_in_pvp = False
            self.current_ante += 1
            await self.send({"action": "setAnte", "ante": self.current_ante})
            await self.send({"action": "newRound"})
            await self.send({"action": "setLocation", "location": "loc_shop-bl_small"})

        # 9. Game Concluded / Returned to Lobby
        elif action in ("stopGame", "winGame", "loseGame"):
            print(f"\n[🏁] Match concluded via {action}! Resetting to lobby...", flush=True)
            await self.reset_to_lobby()

    async def send_ready_lobby(self):
        """Sends the lobby ready packet."""
        self.is_lobby_ready = True
        await self.send({"action": "readyLobby"})

    async def reset_to_lobby(self):
        """Resets match variables and readies up for the next match."""
        self.game_active = False
        self.is_in_pvp = False
        self.current_ante = 1
        if self.pvp_task and not self.pvp_task.done():
            self.pvp_task.cancel()

        # Wait 1.5 seconds for game UI to return to the lobby screen
        await asyncio.sleep(1.5)
        print("[*] Setting READY for the next match in lobby...", flush=True)
        await self.send({"action": "syncClient", "isCached": False})
        await self.send_ready_lobby()

    async def sync_location(self, loc: str):
        blind = "bl_small"
        if "-" in loc:
            blind = loc.split("-", 1)[1]
        self.current_blind = blind

        # Boss Blind / Ready State
        if "ready" in loc or "nemesis" in blind or "boss" in blind or "head" in blind:
            print(f"\n[!] Boss Blind Detected ({loc}). Bot locking in Ready...", flush=True)
            await self.send({"action": "setAnte", "ante": self.current_ante})
            await self.send({"action": "setFurthestBlind", "furthestBlind": blind})
            await self.send({"action": "setLocation", "location": f"loc_ready-{blind}"})
            await self.send({"action": "readyBlind"})
            return

        # Player in Shop
        if "shop" in loc:
            await self.send({"action": "setLocation", "location": loc})
            await self.send({"action": "spentLastShop", "amount": random.randint(8, 20)})
            return

        # Solo Blinds (Small / Big)
        if "playing" in loc:
            await self.send({"action": "setFurthestBlind", "furthestBlind": blind})
            await self.send({"action": "setLocation", "location": loc})
            await asyncio.sleep(1.0)
            score = str(random.randint(1000, 3000) * self.current_ante)
            await self.send({"action": "playHand", "score": score, "handsLeft": 0})
            await self.send({"action": "newRound"})
        elif "selecting" in loc:
            await self.send({"action": "setLocation", "location": loc})
            await self.send({"action": "readyBlind"})

    async def play_pvp_hands(self):
        """Plays all 4 hands consecutively down to 0 hands left."""
        try:
            await self.send({"action": "setLocation", "location": f"loc_playing-{self.current_blind}"})
            
            target = PVP_TARGETS.get(self.current_ante, 2000000)
            final_score = int(target * random.uniform(0.9, 1.3))
            
            total_hands = 4
            current_score = 0

            for h in range(1, total_hands + 1):
                if not self.is_in_pvp:
                    break

                think = random.uniform(3.0, 5.0)
                hands_remaining = total_hands - h
                print(f"    [PvP] Bot playing hand {h}/{total_hands}... (handsLeft: {hands_remaining}) ({think:.1f}s)", flush=True)
                await asyncio.sleep(think)

                if h == total_hands:
                    pts = final_score - current_score
                else:
                    pts = int((final_score / total_hands) * random.uniform(0.7, 1.2))

                current_score += pts

                print(f"    [PvP Play] Score: +{pts:,} -> Total: {current_score:,} (handsLeft: {hands_remaining})", flush=True)
                await self.send({
                    "action": "playHand",
                    "score": str(current_score),
                    "handsLeft": hands_remaining
                })

            print("[✓] Bot finished all hands (handsLeft: 0). Waiting for server endPvP...", flush=True)

        except asyncio.CancelledError:
            pass

    async def interactive_cli(self):
        await asyncio.sleep(1)
        while self.is_running:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            cmd = line.strip().lower()
            if not cmd:
                continue

            if cmd in ("r", "ready"):
                if self.game_active:
                    print("[!] Manually sending readyBlind for current blind...", flush=True)
                    await self.send({"action": "setAnte", "ante": self.current_ante})
                    await self.send({"action": "setFurthestBlind", "furthestBlind": self.current_blind})
                    await self.send({"action": "setLocation", "location": f"loc_ready-{self.current_blind}"})
                    await self.send({"action": "readyBlind"})
                else:
                    print("[!] Manually sending readyLobby...", flush=True)
                    await self.send_ready_lobby()
            elif cmd == "end":
                print("[!] Forcing 0 hands remaining...", flush=True)
                await self.send({"action": "playHand", "score": "50000", "handsLeft": 0})

if __name__ == "__main__":
    code = input("Enter BalatroMP Lobby Code: ").strip()
    if code:
        bot = BalatroSyncBot(name="TrainerBot", color_id=14)
        try:
            asyncio.run(bot.connect(code))
        except KeyboardInterrupt:
            print("\n[*] Bot closed.")
    else:
        print("[Error] Code cannot be empty.")