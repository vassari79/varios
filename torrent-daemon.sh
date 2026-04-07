#!/usr/bin/env bash
# torrent-daemon.sh — start/stop the torrent-queue daemon
# Usage: torrent-daemon.sh {start|stop|restart|status}

DAEMON="${HOME}/bin/torrent-queue.sh"
LOG="/tmp/torrent-queue.log"

case "${1:-status}" in
    start)
        if pgrep -f torrent-queue.sh > /dev/null; then
            echo "Already running ($(pgrep -f torrent-queue.sh))"
        else
            nohup bash "$DAEMON" > "$LOG" 2>&1 &
            echo "Started (PID $!)"
        fi
        ;;
    stop)
        if pkill -f torrent-queue.sh; then
            echo "Stopped"
        else
            echo "Not running"
        fi
        ;;
    restart)
        pkill -f torrent-queue.sh 2>/dev/null; sleep 1
        nohup bash "$DAEMON" > "$LOG" 2>&1 &
        echo "Restarted (PID $!)"
        ;;
    status)
        if pgrep -fa torrent-queue.sh; then
            echo "Queue: $(ls ~/temp/h.temp/torrents/*.torrent 2>/dev/null | wc -l) pending"
        else
            echo "Not running"
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
