import logging

logger = logging.getLogger(__name__)

class HybridRecommendationEngine:
    def __init__(self, db_conn, property_matcher, user_analyzer, homepage_strategy=None):
        self.db_conn = db_conn
        self.property_matcher = property_matcher
        self.user_analyzer = user_analyzer
        self.homepage_strategy = homepage_strategy

    def get_recommendations(self, user_id=None, session_id=None, real_estate_id=None, latitude=0.0, longitude=0.0, limit=10):
        """
        Phối hợp các thuật toán gợi ý dựa trên ngữ cảnh đầu vào và áp dụng Fallback thông minh
        """
        # Kiểm tra xem có đang yêu cầu cho trang tổng ngoài (Homepage) hay không
        # Nếu real_estate_id rỗng hoặc None, điều hướng trực tiếp sang chiến lược trang tổng chuyên biệt
        is_homepage_request = not real_estate_id or str(real_estate_id).strip() == ""

        if is_homepage_request and self.homepage_strategy:
            logger.info("Yêu cầu gợi ý từ trang tổng ngoài (Homepage). Gọi HomepageRecommendationStrategy.")
            return self.homepage_strategy.get_homepage_recommendations(
                user_id=user_id,
                session_id=session_id,
                latitude=latitude,
                longitude=longitude,
                limit=limit
            )

        # TH 1: Có cả lịch sử xem của User và BDS đang xem -> Chạy Hybrid kết hợp (Dành cho trang chi tiết BDS)
        if (user_id or session_id) and real_estate_id:
            content_recs = self.property_matcher.get_recommendations_by_property(
                real_estate_id, latitude, longitude, limit=limit*2
            )
            behavior_recs = self.user_analyzer.get_recommendations_by_behavior(
                user_id=user_id, session_id=session_id, latitude=latitude, longitude=longitude, limit=limit*2
            )

            if content_recs and behavior_recs:
                merged_scores = {}

                # Điểm xếp hạng quy đổi từ thứ hạng xuất hiện (Rank reciprocal)
                for rank, p_id in enumerate(content_recs):
                    score = (1.0 / (rank + 1)) * 0.6
                    merged_scores[p_id] = merged_scores.get(p_id, 0.0) + score

                for rank, p_id in enumerate(behavior_recs):
                    score = (1.0 / (rank + 1)) * 0.4
                    merged_scores[p_id] = merged_scores.get(p_id, 0.0) + score

                # Sắp xếp các BDS gộp giảm dần theo điểm hỗn hợp
                sorted_recs = sorted(merged_scores.items(), key=lambda x: x[1], reverse=True)
                final_ids = [item[0] for item in sorted_recs[:limit]]

                return final_ids, "hybrid"

            if content_recs:
                return content_recs[:limit], "content_based"
            if behavior_recs:
                return behavior_recs[:limit], "behavioral"

        # TH 2: Chỉ có BDS đang xem (Khách vãng lai xem trang chi tiết) -> Dùng Content-Based
        if real_estate_id:
            recs = self.property_matcher.get_recommendations_by_property(
                real_estate_id, latitude, longitude, limit=limit
            )
            if recs:
                return recs, "content_based"

        # TH 3: Fallback dự phòng nếu không khởi tạo homepage_strategy nhưng lại là request trang chủ
        if user_id or session_id:
            recs = self.user_analyzer.get_recommendations_by_behavior(
                user_id=user_id, session_id=session_id, latitude=latitude, longitude=longitude, limit=limit
            )
            if recs:
                return recs, "behavioral"

        # TH 4: Cold-Start (Mọi nguồn dữ liệu đều trống hoặc rỗng) -> Kích hoạt Fallback
        logger.info("Không đủ dữ liệu đầu vào hoặc các chiến lược rỗng. Kích hoạt Fallback.")
        fallback_recs = self._get_fallback_recommendations(latitude, longitude, limit)
        return fallback_recs, "fallback"

    def compute_user_recommendations(self, user_id, view_history, latitude=0.0, longitude=0.0, limit=20):
        """
        Tính toán gợi ý cho tác vụ precompute (Background Job) sử dụng lịch sử xem được truyền trực tiếp
        """
        recs = self.user_analyzer.get_recommendations_by_behavior(
            user_id=user_id, view_history=view_history, latitude=latitude, longitude=longitude, limit=limit
        )
        if recs:
            return recs, "behavioral_precomputed"

        fallback_recs = self._get_fallback_recommendations(latitude, longitude, limit)
        return fallback_recs, "fallback_precomputed"

    def _get_fallback_recommendations(self, latitude=0.0, longitude=0.0, limit=10):
        """
        Trả về danh sách bất động sản phổ biến nhất theo khu vực địa lý hoặc toàn cục hệ thống
        """
        cursor = self.db_conn.cursor(dictionary=True)
        try:
            # Ưu tiên theo vị trí địa lý (Local Trending trong bán kính 10km)
            if latitude != 0.0 and longitude != 0.0:
                query_local_trending = """
                    SELECT p.id, COUNT(vh.id) as view_count
                    FROM properties p
                    LEFT JOIN view_history vh ON p.id = vh.real_estate_id AND vh.created_at >= NOW() - INTERVAL 7 DAY
                    WHERE p.status = 'active'
                      AND (6371 * acos(
                            cos(radians(%s)) * cos(radians(p.latitude)) *
                            cos(radians(p.longitude) - radians(%s)) +
                            sin(radians(%s)) * sin(radians(p.latitude))
                          )) <= 10.0
                    GROUP BY p.id
                    ORDER BY view_count DESC, p.created_at DESC
                    LIMIT %s
                """
                cursor.execute(query_local_trending, (latitude, longitude, latitude, limit))
                results = cursor.fetchall()
                if results:
                    return [item['id'] for item in results]

            # Trending toàn cục trong vòng 7 ngày qua
            query_global_trending = """
                SELECT p.id, COUNT(vh.id) as view_count
                FROM properties p
                LEFT JOIN view_history vh ON p.id = vh.real_estate_id AND vh.created_at >= NOW() - INTERVAL 7 DAY
                WHERE p.status = 'active'
                GROUP BY p.id
                ORDER BY view_count DESC, p.created_at DESC
                LIMIT %s
            """
            cursor.execute(query_global_trending, (limit,))
            results = cursor.fetchall()
            return [item['id'] for item in results]

        except Exception as e:
            logger.error(f"Lỗi khi truy vấn fallback recommendations: {str(e)}")
            return []
        finally:
            cursor.close()
