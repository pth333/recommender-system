import sys
import os
import grpc
import logging
import signal
from concurrent import futures
import mysql.connector
import redis

# Thêm thư mục hiện tại và thư mục proto vào sys.path để giải quyết import
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
sys.path.append(os.path.join(current_dir, "proto"))

from proto import recommendation_pb2_grpc
from recommendation_grpc_handler import RecommendationGRPCHandler

# Thiết lập hệ thống ghi log
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class GRPCServer:
    def __init__(self, port="50051"):
        self.port = port
        self.server = None
        self.db_conn = None
        self.redis_client = None

    def init_dependencies(self):
        """
        Khởi tạo kết nối MySQL và Redis
        """
        try:
            # Kết nối database MySQL (ML Service chỉ cần quyền Read-Only)
            self.db_conn = mysql.connector.connect(
                host=os.getenv("DB_HOST", "localhost"),
                port=3307,
                user=os.getenv("DB_USER", "root"),
                password=os.getenv("DB_PASSWORD", "1111"),
                database=os.getenv("DB_NAME", "real_estate_db"),
                # pool_name="ml_pool",
                pool_size=10
            )
            logger.info("Kết nối MySQL Database thành công (connection pool size=10)")

            # Kết nối Redis
            self.redis_client = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                db=int(os.getenv("REDIS_DB", 0)),
                decode_responses=True
            )
            self.redis_client.ping()
            logger.info("Kết nối Redis thành công")

        except Exception as e:
            logger.error(f"Lỗi khởi tạo kết nối ngoại vi: {str(e)}")
            raise e

    def serve(self):
        """
        Khởi chạy gRPC Server
        """
        self.init_dependencies()

        # Tạo gRPC Server với Thread Pool để xử lý song song các requests
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

        # Đăng ký servicer xử lý chính
        servicer = RecommendationGRPCHandler(self.db_conn, self.redis_client)
        recommendation_pb2_grpc.add_RecommendationServiceServicer_to_server(servicer, self.server)

        # Lắng nghe cổng kết nối
        server_address = f"0.0.0.0:{self.port}"
        self.server.add_insecure_port(server_address)
        logger.info(f"Đang chạy gRPC Server trên cổng: {self.port}")
        self.server.start()

        # Đăng ký xử lý tín hiệu kết thúc ứng dụng một cách an toàn (Graceful Shutdown)
        def handle_shutdown(signum, frame):
            logger.info("Nhận tín hiệu dừng hệ thống, đang tắt gRPC Server...")
            self.server.stop(5)  # Chờ tối đa 5 giây cho các request hiện tại hoàn tất

            if self.db_conn:
                self.db_conn.close()
                logger.info("Đã đóng kết nối database MySQL")

            logger.info("Đã tắt gRPC Server an toàn")
            sys.exit(0)

        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)

        # Giữ main thread chạy liên tục cho tới khi kết thúc
        self.server.wait_for_termination()

if __name__ == "__main__":
    server = GRPCServer()
    server.serve()
