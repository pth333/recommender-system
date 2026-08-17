import os
import sys
import time
import json
import logging
import mysql.connector
import redis
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler

# Thêm đường dẫn tuyệt đối để giải quyết import
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

from strategies.user_interest_analyzer import UserInterestAnalyzer

# Thiết lập hệ thống log riêng cho Batch Job
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("PrecomputeScheduler")

class PrecomputeScheduler:
    def __init__(self):
        self.db_conn = None
        self.redis_client = None
        self.user_analyzer = None

    def init_connections(self):
        """
        Khởi tạo kết nối đến các tài nguyên ngoài
        """
        try:
            # Kết nối MySQL (Khởi tạo kết nối mới mỗi chu kỳ chạy để tránh lỗi idle timeout của MySQL)
            self.db_conn = mysql.connector.connect(
                host=os.getenv("DB_HOST", "localhost"),
                user=os.getenv("DB_USER", "ml_user"),
                password=os.getenv("DB_PASSWORD", "ml_password"),
                database=os.getenv("DB_NAME", "real_estate_db")
            )

            # Kết nối Redis
            self.redis_client = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                db=int(os.getenv("REDIS_DB", 0)),
                decode_responses=True
            )
            self.redis_client.ping()

            # Khởi tạo chiến lược hành vi mới để phân tích
            self.user_analyzer = UserInterestAnalyzer(self.db_conn)
            logger.info("Khởi tạo scheduler thành công với các kết nối cơ sở dữ liệu và Redis.")
        except Exception as e:
            logger.error(f"Không thể khởi tạo các kết nối: {str(e)}")
            raise e

    def close_connections(self):
        """
        Đóng dọn dẹp các kết nối sau khi hoàn thành chu kỳ tính toán
        """
        try:
            if self.db_conn and self.db_conn.is_connected():
                self.db_conn.close()
                logger.info("Đã đóng kết nối MySQL thành công.")
        except Exception as e:
            logger.error(f"Lỗi khi dọn dẹp kết nối: {str(e)}")

    def run_precomputation_job(self):
        """
        Công việc tính toán trước danh sách gợi ý cho tất cả người dùng hoạt động trong 24h qua
        """
        start_time = time.time()
        logger.info("Bắt đầu thực hiện chu kỳ Precomputations cho người dùng hoạt động...")

        try:
            self.init_connections()
        except Exception:
            logger.error("Bỏ qua chu kỳ precompute do lỗi kết nối tài nguyên.")
            return

        cursor = self.db_conn.cursor(dictionary=True)
        try:
            # 1. Tìm danh sách người dùng đã có tương tác xem bất động sản trong 24 giờ qua
            query_active_users = """
                SELECT DISTINCT user_id
                FROM view_history
                WHERE created_at >= NOW() - INTERVAL 1 DAY
                  AND user_id IS NOT NULL
            """
            cursor.execute(query_active_users)
            users = cursor.fetchall()

            total_users = len(users)
            logger.info(f"Tìm thấy {total_users} người dùng đang hoạt động trong vòng 24h qua.")

            if total_users == 0:
                logger.info("Không có người dùng nào hoạt động, kết thúc chu kỳ precompute.")
                return

            # Sử dụng Redis Pipeline để tối ưu số lần gửi yêu cầu ghi qua mạng (bulk writes)
            pipeline = self.redis_client.pipeline()
            processed_count = 0
            error_count = 0

            # 2. Với từng người dùng, tiến hành chạy đề xuất
            for user in users:
                user_id = user['user_id']
                try:
                    # Truy vấn 20 lịch sử xem gần nhất của người dùng này để đưa vào thuật toán
                    query_history = """
                        SELECT real_estate_id, duration_seconds, UNIX_TIMESTAMP(created_at) as viewed_at_unix
                        FROM view_history
                        WHERE user_id = %s
                        ORDER BY created_at DESC
                        LIMIT 20
                    """
                    # Sử dụng cursor phụ riêng biệt để không xung đột với luồng lặp chính
                    hist_cursor = self.db_conn.cursor(dictionary=True)
                    hist_cursor.execute(query_history, (user_id,))
                    view_history = hist_cursor.fetchall()
                    hist_cursor.close()

                    if not view_history:
                        continue

                    # Gọi trực tiếp UserInterestAnalyzer để tính toán trước top 20 gợi ý tốt nhất
                    property_ids = self.user_analyzer.get_recommendations_by_behavior(
                        user_id=user_id,
                        view_history=view_history,
                        limit=20
                    )

                    if property_ids:
                        # Ghi thẳng vào Redis Pipeline với TTL = 2 tiếng (7200 giây)
                        redis_key = f"rec:user:{user_id}"
                        pipeline.setex(redis_key, 7200, json.dumps(property_ids))
                        processed_count += 1

                except Exception as user_err:
                    logger.error(f"Lỗi khi tính toán trước cho user={user_id}: {str(user_err)}")
                    error_count += 1

                # Thực thi lưu trữ lũy tiến mỗi lô 50 người dùng để tránh nghẽn luồng truyền mạng
                if processed_count % 50 == 0 and processed_count > 0:
                    pipeline.execute()
                    logger.info(f"Đã lưu trữ lũy tiến {processed_count} người dùng vào Redis.")

            # Gửi các lệnh ghi còn lại trong pipeline
            pipeline.execute()

            elapsed_time = time.time() - start_time
            logger.info(f"Chu kỳ Precomputation kết thúc trong {elapsed_time:.2f}s.")
            logger.info(f"Tóm tắt: Hoàn thành={processed_count}/{total_users} người dùng, Lỗi={error_count}.")

        except Exception as job_err:
            logger.error(f"Lỗi nghiêm trọng trong quá trình chạy Precomputation Job: {str(job_err)}")
        finally:
            cursor.close()
            self.close_connections()

def start_scheduler():
    """
    Điểm khởi tạo scheduler để kích hoạt tác vụ chạy ngầm định kỳ
    """
    scheduler = BlockingScheduler()
    precomputer = PrecomputeScheduler()

    # Đăng ký task chạy định kỳ 30 phút một lần
    scheduler.add_job(
        precomputer.run_precomputation_job,
        'interval',
        minutes=30,
        next_run_time=datetime.now() + timedelta(seconds=10) # Lần chạy thử đầu tiên sau 10 giây khi khởi động
    )

    logger.info("Đã kích hoạt APScheduler background precompute job (chu kỳ: 30 phút).")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Đã tắt Scheduler an toàn.")

if __name__ == "__main__":
    start_scheduler()
