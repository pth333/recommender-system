import grpc
import time
import json
import logging

from proto import recommendation_pb2
from proto import recommendation_pb2_grpc
from strategies.property_attribute_matcher import PropertyAttributeMatcher
from strategies.user_interest_analyzer import UserInterestAnalyzer
from strategies.homepage_recommendation_strategy import HomepageRecommendationStrategy
from strategies.hybrid_recommendation_engine import HybridRecommendationEngine

logger = logging.getLogger(__name__)

class RecommendationGRPCHandler(recommendation_pb2_grpc.RecommendationServiceServicer):
    """
    Class xử lý các yêu cầu gRPC từ Go Backend gửi tới dịch vụ ML
    """
    def __init__(self, db_conn, redis_client):
        self.db_conn = db_conn
        self.redis_client = redis_client

        # Khởi tạo các lớp xử lý thuật toán mới
        self.property_matcher = PropertyAttributeMatcher(db_conn)
        self.user_analyzer = UserInterestAnalyzer(db_conn)
        self.homepage_strategy = HomepageRecommendationStrategy(db_conn, self.user_analyzer)

        # Truyền homepage_strategy vào hybrid engine để xử lý riêng biệt cho trang tổng
        self.hybrid_engine = HybridRecommendationEngine(
            db_conn,
            self.property_matcher,
            self.user_analyzer,
            self.homepage_strategy
        )

    def GetRecommendations(self, request, context):
        """
        Xử lý yêu cầu lấy danh sách gợi ý bất động sản (Real-time)
        """
        start_time = time.time()
        try:
            user_id = request.user_id
            session_id = request.session_id
            real_estate_id = request.real_estate_id
            latitude = request.latitude
            longitude = request.longitude
            limit = request.limit if request.limit > 0 else 10

            logger.info(f"Nhận yêu cầu gợi ý: user_id={user_id}, session_id={session_id}, property_id={real_estate_id}")

            property_ids, strategy = self.hybrid_engine.get_recommendations(
                user_id=user_id,
                session_id=session_id,
                real_estate_id=real_estate_id,
                latitude=latitude,
                longitude=longitude,
                limit=limit
            )

            duration = (time.time() - start_time) * 1000
            logger.info(f"Hoàn thành GetRecommendations trong {duration:.2f}ms với chiến lược {strategy}")

            return recommendation_pb2.RecommendResponse(
                property_ids=property_ids,
                strategy=strategy
            )

        except Exception as e:
            logger.error(f"Lỗi khi xử lý GetRecommendations: {str(e)}", exc_info=True)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("Lỗi hệ thống nội bộ khi tính toán gợi ý")
            return recommendation_pb2.RecommendResponse(property_ids=[], strategy="error_fallback")

    def PrecomputeForUser(self, request, context):
        """
        Xử lý yêu cầu tính toán trước gợi ý cho người dùng từ background job
        """
        start_time = time.time()
        try:
            user_id = request.user_id
            view_history = request.view_history
            latitude = request.latitude
            longitude = request.longitude

            logger.info(f"Bắt đầu Precompute cho user={user_id} với {len(view_history)} lịch sử xem")

            if not user_id:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("user_id không được để trống")
                return recommendation_pb2.PrecomputeResponse(success=False, count=0)

            formatted_history = []
            for v in view_history:
                formatted_history.append({
                    'real_estate_id': v.real_estate_id,
                    'duration': v.duration,
                    'viewed_at_unix': v.viewed_at_unix
                })

            property_ids, _ = self.hybrid_engine.compute_user_recommendations(
                user_id=user_id,
                view_history=formatted_history,
                latitude=latitude,
                longitude=longitude,
                limit=20
            )

            if property_ids:
                redis_key = f"rec:user:{user_id}"
                self.redis_client.setex(
                    redis_key,
                    7200,  # TTL = 2 giờ cho dữ liệu được precompute
                    json.dumps(property_ids)
                )

            duration = time.time() - start_time
            logger.info(f"Hoàn thành Precompute cho user={user_id} trong {duration:.2f}s, tìm thấy {len(property_ids)} BDS")

            return recommendation_pb2.PrecomputeResponse(
                success=True,
                count=len(property_ids)
            )

        except Exception as e:
            logger.error(f"Lỗi khi xử lý PrecomputeForUser: {str(e)}", exc_info=True)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("Lỗi hệ thống nội bộ khi chạy precompute")
            return recommendation_pb2.PrecomputeResponse(success=False, count=0)
