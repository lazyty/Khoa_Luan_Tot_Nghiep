#!/bin/sh
ACTION=$1
TARGET=$2
NFT_TABLE="inet filter"
NFT_CHAIN="input"
LOG_BANNED="/var/log/banned_ips.log"
LOG_REMEDIATION="/var/log/remediation.log"

if [ "$(id -u)" -ne 0 ]; then
    NFT="sudo nft"
    RC="sudo rc-service"
else
    NFT="nft"
    RC="rc-service"
fi

case $ACTION in
    block_ip)
        if [ -z "$TARGET" ]; then
            echo "Error: No IP specified"
            exit 1
        fi
        if $NFT list chain $NFT_TABLE $NFT_CHAIN 2>/dev/null | grep -q "ip saddr $TARGET drop"; then
            echo "IP $TARGET đã bị chặn từ trước."
        else
            $NFT add rule $NFT_TABLE $NFT_CHAIN ip saddr "$TARGET" drop

            if [ $? -eq 0 ]; then
                echo "$(date '+%Y-%m-%d %H:%M:%S') | BLOCKED | IP: $TARGET | Host: $(hostname)" >> "$LOG_BANNED"
                echo "Blocked IP thành công: $TARGET"
            else
                echo "Error: Không thể block IP $TARGET (kiểm tra quyền hoặc nft table/chain)" >&2
                exit 1
            fi
        fi
        ;;
    unblock_ip)
        if [ -z "$TARGET" ]; then
            echo "Error: No IP specified"
            exit 1
        fi
        HANDLE_IDS=$(
            $NFT -a list chain $NFT_TABLE $NFT_CHAIN 2>/dev/null \
            | grep "ip saddr $TARGET drop" \
            | sed 's/.*handle[[:space:]]*//'
        )
        if [ -z "$HANDLE_IDS" ]; then
            echo "Không tìm thấy luật chặn nào cho IP: $TARGET"
        else
            FAILED=0

            for HANDLE in $HANDLE_IDS; do
                $NFT delete rule $NFT_TABLE $NFT_CHAIN handle "$HANDLE"

                if [ $? -ne 0 ]; then
                    echo "Error: Không thể xóa handle $HANDLE" >&2
                    FAILED=1
                fi
            done

            if [ $FAILED -eq 0 ]; then
                echo "$(date '+%Y-%m-%d %H:%M:%S') | UNBLOCKED | IP: $TARGET | Host: $(hostname)" >> "$LOG_REMEDIATION"
                echo "Unblocked IP thành công: $TARGET"
            fi
        fi
        ;;
    restart)
        if [ -z "$TARGET" ]; then
            echo "Error: No service specified"
            exit 1
        fi
        case $TARGET in
            http|https|nginx)
                REAL_SERVICE="nginx"
                ;;
            dns|dnsmasq)
                REAL_SERVICE="dnsmasq"
                ;;
            ssh|sshd)
                REAL_SERVICE="sshd"
                ;;

            *)
                REAL_SERVICE="$TARGET"
                ;;
        esac
        $RC "$REAL_SERVICE" restart
        if [ $? -eq 0 ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') | RESTARTED | Service: $REAL_SERVICE (Alias: $TARGET)"
            echo "Restarted service thành công: $REAL_SERVICE"
        else
            echo "Error: Không thể restart $REAL_SERVICE" >&2
            exit 1
        fi
        ;;

    *)
        echo "Usage: $0 [block_ip|unblock_ip|restart] [IP|service]"
        echo "Ví dụ: $0 restart http"
        echo "Ví dụ: $0 block_ip 192.168.1.99"
        exit 1
        ;;
esac