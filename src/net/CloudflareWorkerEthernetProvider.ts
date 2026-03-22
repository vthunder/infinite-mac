import {
    type EmulatorEthernetProvider,
    type EmulatorEthernetProviderDelegate,
} from "@/emulator/ui/ui";

/**
 * Works in conjunction with the Cloudflare worker defined in
 * workers-ethernet/src/index.mjs to broadcast Ethernet packets to all emulator
 * instances in the same named zone.
 */
export class CloudflareWorkerEthernetProvider
    implements EmulatorEthernetProvider
{
    #zoneName: string;
    #wsUrlOverride?: string;
    #macAddress?: string;
    #webSocket: WebSocket;
    #delegate?: EmulatorEthernetProviderDelegate;
    #state: "opening" | "closed" | "opened" = "opening";
    #bufferedMessages: string[] = [];
    #reconnectTimeout?: number;

    constructor(zoneName: string, wsUrlOverride?: string) {
        this.#zoneName = zoneName;
        this.#wsUrlOverride = wsUrlOverride;
        this.#webSocket = this.#connect();
    }

    wsUrlOverride(): string | undefined {
        return this.#wsUrlOverride;
    }

    description(): string {
        return `Zone ${this.#zoneName}`;
    }

    zoneName(): string {
        return this.#zoneName;
    }

    macAddress(): string | undefined {
        return this.#macAddress;
    }

    #connect(): WebSocket {
        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        const origin = `${protocol}//${location.host}`;
        const url =
            this.#wsUrlOverride ??
            `${origin}/zone/${this.#zoneName}/websocket`;
        const webSocket = new WebSocket(url);
        webSocket.addEventListener("open", this.#handleOpen);
        webSocket.addEventListener("close", this.#handleClose);
        webSocket.addEventListener("error", this.#handleError);
        webSocket.addEventListener("message", this.#handleMessage);
        return webSocket;
    }

    #reconnect(): void {
        const webSocket = this.#webSocket;
        webSocket.removeEventListener("open", this.#handleOpen);
        webSocket.removeEventListener("close", this.#handleClose);
        webSocket.removeEventListener("error", this.#handleError);
        webSocket.removeEventListener("message", this.#handleMessage);

        this.#state = "opening";
        if (this.#reconnectTimeout) {
            window.clearTimeout(this.#reconnectTimeout);
        }
        this.#reconnectTimeout = window.setTimeout(() => {
            this.#webSocket = this.#connect();
        }, 1000);
    }

    init(macAddress: string): void {
        this.#macAddress = macAddress;
        this.#send({type: "init", macAddress});
    }

    close() {
        this.#state = "closed";
        this.#send({type: "close"});
        this.#webSocket.close();
    }

    send(destination: string, packet: Uint8Array): void {
        // TODO: send packets directly as Uint8Arrays, to avoid copying overhead.
        this.#send({
            type: "send",
            destination,
            packetArray: Array.from(packet),
        });
    }

    #send(message: any) {
        message = JSON.stringify(message);
        if (this.#state === "opened") {
            this.#webSocket.send(message);
        } else {
            this.#bufferedMessages.push(message);
        }
    }

    setDelegate(delegate: EmulatorEthernetProviderDelegate): void {
        this.#delegate = delegate;
    }

    #handleOpen = (event: Event): void => {
        this.#state = "opened";
        const bufferedMessages = this.#bufferedMessages;
        this.#bufferedMessages = [];
        for (const message of bufferedMessages) {
            this.#webSocket.send(message);
        }
        if (this.#macAddress) {
            this.init(this.#macAddress);
        }
    };

    #handleClose = (event: CloseEvent): void => {
        if (this.#state === "closed") {
            // Intentionally closed
            return;
        }
        this.#reconnect();
    };

    #handleError = (event: Event): void => {
        console.error("WebSocket error", event);
        this.#reconnect();
    };

    #handleMessage = (event: MessageEvent): void => {
        const data = JSON.parse(event.data);
        const {type} = data;
        switch (type) {
            case "receive": {
                const {packetArray} = data;
                const packet = new Uint8Array(packetArray);
                if (packet.length >= 14) {
                    const etherType = (packet[12] << 8) | packet[13];
                    const dst = Array.from(packet.slice(0, 6)).map(b => b.toString(16).padStart(2, '0')).join(':');
                    const src = Array.from(packet.slice(6, 12)).map(b => b.toString(16).padStart(2, '0')).join(':');
                    let extra: Record<string, unknown> = {};
                    if (etherType === 0x0806 && packet.length >= 42) {
                        const op = (packet[20] << 8) | packet[21];
                        extra.arp = {op: op === 1 ? 'request' : 'reply', senderIP: `${packet[28]}.${packet[29]}.${packet[30]}.${packet[31]}`, targetIP: `${packet[38]}.${packet[39]}.${packet[40]}.${packet[41]}`};
                    } else if (etherType === 0x0800 && packet.length >= 34) {
                        const proto = packet[23];
                        extra.ipv4 = {proto: proto === 6 ? 'TCP' : proto === 17 ? 'UDP' : proto, srcIP: `${packet[26]}.${packet[27]}.${packet[28]}.${packet[29]}`, dstIP: `${packet[30]}.${packet[31]}.${packet[32]}.${packet[33]}`};
                        if (proto === 6 && packet.length >= 54) extra.tcp = {srcPort: (packet[34] << 8) | packet[35], dstPort: (packet[36] << 8) | packet[37], flags: (packet[47] & 0x02 ? 'SYN' : '') + (packet[47] & 0x10 ? 'ACK' : '') || 'other'};
                    }
                    console.log(`[ethernet-in] ${JSON.stringify({dst, src, etherType: '0x' + etherType.toString(16), len: packet.length, ...extra})}`);
                }
                this.#delegate?.receive(packet);
                break;
            }
            case "nav": {
                // Server intercepted an HTTP request to 10.0.0.1/nav/* and is
                // asking us to navigate. Relay to the parent frame.
                const {path} = data;
                if (typeof path === "string") {
                    window.parent.postMessage(
                        {type: "emulator_nav", path},
                        "*"
                    );
                }
                break;
            }
        }
    };
}
