import math
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class UserInterestAnalyzer:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def _get_user_view_history_from_db(self, user_id=None, session_id=None):
        """
        Truy vấn lịch sử xem của người dùng từ database (nếu không có sẵn trong request)
        """
        cursor = self.db_conn.cursor(dictionary=True)
        try:
            if user_id:
                query = """
                    SELECT real_estate_id, duration_seconds, UNIX_TIMESTAMP(created_at) as viewed_at_unix
                    FROM view_history
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 20
                """
                cursor.execute(query, (user_id,))
            elif session_id:
                query = """
                    SELECT real_estate_id, duration_seconds, UNIX_TIMESTAMP(created_at) as viewed_at_unix
                    FROM view_history
                    WHERE session_id = %s
                    ORDER BY created_at DESC
                    LIMIT 20
                """
                cursor.execute(query, (session_id,))
            else:
                return []
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Lỗi khi truy vấn lịch sử xem của user: {str(e)}")
            return []
        finally:
            cursor.close()

    def get_recommendations_by_behavior(self, user_id=None, session_id=None, view_history=None, latitude=0.0, longitude=0.0, limit=10):
        """
        Gợi ý dựa trên hành vi tương tác xem trong quá khứ
        """
        if not view_history:
            raw_history = self._get_user_view_history_from_db(user_id, session_id)
            if not raw_history:
                return []
            view_history = raw_history

        prop_ids = [item['real_estate_id'] for item in view_history]
        if not prop_ids:
            return []

        cursor = self.db_conn.cursor(dictionary=True)
        try:
            # Lấy thông tin thuộc tính của các BDS người dùng đã xem
            format_strings = ','.join(['%s'] * len(prop_ids))
            query_props = f"""
                SELECT id, category, price, area, district_id, city_id
                FROM properties
                WHERE id IN ({format_strings}) AND status = 'active'
            """
            cursor.execute(query_props, tuple(prop_ids))
            properties_data = {row['id']: row for row in cursor.fetchall()}

            if not properties_data:
                return []

            # Thiết lập biến tính toán profile người dùng
            now_unix = datetime.utcnow().timestamp()
            lambda_decay = 0.1  # Hệ số suy giảm theo thời gian 10% mỗi ngày

            user_profile = {
                "categories": {},
                "districts": {},
                "weighted_price": 0.0,
                "weighted_area": 0.0,
                "total_weight": 0.0
            }

            for view in view_history:
                prop_id = view['real_estate_id']
                if prop_id not in properties_data:
                    continue

                prop = properties_data[prop_id]
                duration = view['duration'] if 'duration' in view else view.get('duration_seconds', 10)
                viewed_at = view['viewed_at_unix'] if 'viewed_at_unix' in view else view.get('viewed_at_unix', now_unix)

                # Chống click ảo (>5s) và khống chế treo tab (<3600s)
                duration = max(5, min(3600, duration))
                duration_weight = math.log(duration + 1)

                # Tính toán time decay
                days_ago = max(0.0, (now_unix - viewed_at) / 86400.0)
                time_decay = math.exp(-lambda_decay * days_ago)

                weight = duration_weight * time_decay

                # Tích lũy sở thích
                cat = prop['category']
                user_profile["categories"][cat] = user_profile["categories"].get(cat, 0.0) + weight

                dist = prop['district_id']
                user_profile["districts"][dist] = user_profile["districts"].get(dist, 0.0) + weight

                user_profile["weighted_price"] += float(prop['price']) * weight
                user_profile["weighted_area"] += float(prop['area']) * weight
                user_profile["total_weight"] += weight

            if user_profile["total_weight"] == 0:
                return []

            # Điểm trung bình có trọng số của phân khúc giá và diện tích người dùng thích
            avg_price = user_profile["weighted_price"] / user_profile["total_weight"]
            avg_area = user_profile["weighted_area"] / user_profile["total_weight"]

            # Lấy danh mục yêu thích và top 3 quận quan tâm nhiều nhất
            favorite_category = max(user_profile["categories"], key=user_profile["categories"].get)
            sorted_districts = sorted(user_profile["districts"].items(), key=lambda x: x[1], reverse=True)
            favorite_districts = [item[0] for item in sorted_districts[:3]]

            # Tìm kiếm ứng viên (loại trừ các BDS người dùng đã xem)
            excluded_ids_placeholder = ','.join(['%s'] * len(prop_ids))
            price_min, price_max = avg_price * 0.6, avg_price * 1.4
            area_min, area_max = avg_area * 0.5, avg_area * 1.5

            district_clause = ""
            district_params = []
            if favorite_districts:
                dist_placeholders = ','.join(['%s'] * len(favorite_districts))
                district_clause = f"OR district_id IN ({dist_placeholders})"
                district_params = favorite_districts

            query_candidates = f"""
                SELECT id, category, price, area, district_id, latitude, longitude
                FROM properties
                WHERE id NOT IN ({excluded_ids_placeholder})
                  AND status = 'active'
                  AND category = %s
                  AND (price BETWEEN %s AND %s OR area BETWEEN %s AND %s {district_clause})
                LIMIT 100
            """

            query_params = list(prop_ids) + [favorite_category, price_min, price_max, area_min, area_max] + district_params
            cursor.execute(query_candidates, query_params)
            candidates = cursor.fetchall()

            # Tính điểm Behavioral Score cho các ứng viên
            scored_candidates = []
            for item in candidates:
                # 1. Khớp Category (Trọng số 30%)
                cat_score = 1.0 if item['category'] == favorite_category else 0.0

                # 2. Khớp Quận/Huyện (Trọng số 30%)
                dist_score = 0.0
                if favorite_districts:
                    if item['district_id'] == favorite_districts[0]:
                        dist_score = 1.0
                    elif item['district_id'] in favorite_districts:
                        dist_score = 0.6

                # 3. Tiệm cận phân khúc ưa thích (Trọng số 40%)
                price_diff_ratio = abs(float(item['price']) - avg_price) / avg_price
                price_match = max(0.0, 1.0 - (price_diff_ratio / 0.4))

                area_diff_ratio = abs(float(item['area']) - avg_area) / avg_area
                area_match = max(0.0, 1.0 - (area_diff_ratio / 0.5))

                profile_score = (price_match * 0.5) + (area_match * 0.5)

                behavior_score = (cat_score * 0.3) + (dist_score * 0.3) + (profile_score * 0.4)
                scored_candidates.append((item['id'], behavior_score))

            scored_candidates.sort(key=lambda x: x[1], reverse=True)
            return [item[0] for item in scored_candidates[:limit]]

        except Exception as e:
            logger.error(f"Lỗi khi tính Behavioral recommendations: {str(e)}")
            return []
        finally:
            cursor.close()
