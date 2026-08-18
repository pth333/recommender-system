import logging
import math

logger = logging.getLogger(__name__)

class HomepageRecommendationStrategy:
    def __init__(self, db_conn, user_analyzer):
        self.db_conn = db_conn
        self.user_analyzer = user_analyzer

    def _get_user_search_keywords(self, user_id=None):
        """
        Truy vấn các từ khóa tìm kiếm gần đây nhất của người dùng từ search_history
        """
        if not user_id:
            return []
        cursor = self.db_conn.cursor(dictionary=True)
        try:
            query = """
                SELECT query FROM search_history
                WHERE user_id = %s
                ORDER BY searched_at DESC
                LIMIT 5
            """
            cursor.execute(query, (user_id,))
            return [row['query'] for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Lỗi khi lấy lịch sử tìm kiếm: {str(e)}")
            return []
        finally:
            cursor.close()

    def get_homepage_recommendations(self, user_id=None, session_id=None, latitude=0.0, longitude=0.0, limit=10):
        """
        Tính toán gợi ý tối ưu cho trang tổng ngoài (Homepage) kết hợp hành vi xem, định vị và lịch sử tìm kiếm
        """
        cursor = self.db_conn.cursor(dictionary=True)
        try:
            # 1. Thử lấy gợi ý dựa vào hành vi xem tích lũy (Sở thích phân khúc)
            behavior_recs = self.user_analyzer.get_recommendations_by_behavior(
                user_id=user_id, session_id=session_id, latitude=latitude, longitude=longitude, limit=limit*2
            )

            # 2. Lấy từ khóa tìm kiếm gần nhất
            search_queries = self._get_user_search_keywords(user_id)

            # 3. Phân tích từ khóa để lọc thêm ứng viên (so khớp LIKE cơ bản với tiêu đề/mô tả BDS)
            search_matched_recs = []
            if search_queries:
                like_clauses = " OR ".join(["title LIKE %s OR description LIKE %s"] * len(search_queries))
                params = []
                for q in search_queries:
                    params.extend([f"%{q}%", f"%{q}%"])

                query_search_candidates = f"""
                    SELECT id FROM real_estates
                    WHERE ({like_clauses})
                    ORDER BY id DESC
                    LIMIT {limit*2}
                """
                cursor.execute(query_search_candidates, tuple(params))
                search_matched_recs = [row['id'] for row in cursor.fetchall()]

            # 4. (Tạm bỏ GPS theo yêu cầu)
            # 5. Phối hợp chấm điểm và sắp xếp kết quả hỗn hợp
            # Trọng số: Hành vi xem (60%), Lịch sử tìm kiếm (40%)
            final_scores = {}

            for rank, p_id in enumerate(behavior_recs):
                score = (1.0 / (rank + 1)) * 0.6
                final_scores[p_id] = final_scores.get(p_id, 0.0) + score

            for rank, p_id in enumerate(search_matched_recs):
                score = (1.0 / (rank + 1)) * 0.4
                final_scores[p_id] = final_scores.get(p_id, 0.0) + score

            # Sắp xếp kết quả tổng hợp giảm dần
            sorted_recs = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)
            real_estate_ids = [item[0] for item in sorted_recs[:limit]]
            print("Real Estate: ", real_estate_ids)

            if real_estate_ids:
                return real_estate_ids, "homepage_personalized"

            # 6. Fallback nếu không có dữ liệu người dùng (Cold Start) -> Trả về BDS mới đăng thịnh hành
            query_trending = """
                SELECT p.id, COUNT(vh.id) as view_count
                FROM real_estates p
                LEFT JOIN view_history vh ON p.id = vh.real_estate_id AND vh.created_at >= NOW() - INTERVAL 7 DAY
                WHERE p.status = 'active'
                GROUP BY p.id
                ORDER BY view_count DESC, p.created_at DESC
                LIMIT %s
            """
            cursor.execute(query_trending, (limit,))
            fallback_recs = [row['id'] for row in cursor.fetchall()]

            return [row['id'] for row in fallback_recs], "homepage_fallback_trending"

        except Exception as e:
            logger.error(f"Lỗi khi xử lý gợi ý trang tổng: {str(e)}")
            return [], "homepage_error_fallback"
        finally:
            cursor.close()
