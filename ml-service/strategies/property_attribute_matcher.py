import math
import logging

logger = logging.getLogger(__name__)

class PropertyAttributeMatcher:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def get_recommendations_by_property(self, real_estate_id, latitude=0.0, longitude=0.0, limit=10):
        """
        Lấy các bất động sản tương tự với một bất động sản cụ thể dựa trên thuộc tính
        """
        cursor = self.db_conn.cursor(dictionary=True)
        try:
            # Bước 1: Lấy thông tin thuộc tính của BDS gốc làm mốc so sánh
            query_base = """
                SELECT id, category_id, price_vnd, acreage, project_id, district, city
                FROM real_esates
                WHERE id = %s
            """
            cursor.execute(query_base, (real_estate_id,))
            base_prop = cursor.fetchone()

            if not base_prop:
                logger.warning(f"Không tìm thấy thông tin cho bất động sản gốc: {real_estate_id}")
                return []

            # Bước 2: Tìm các bất động sản ứng viên thỏa mãn bộ lọc giá/diện tích
            price_min = float(base_prop['price_vnd']) * 0.7
            price_max = float(base_prop['price_vnd']) * 1.3
            area_min = float(base_prop['acreage']) * 0.6
            area_max = float(base_prop['acreage']) * 1.4

            # Tính toán khoảng cách địa lý (Haversine Formula) trực tiếp trong SQL
            # ref_lat = latitude if latitude != 0.0 else float(base_prop['latitude'] or 0.0)
            # ref_lng = longitude if longitude != 0.0 else float(base_prop['longitude'] or 0.0)

            query_candidates = """
                SELECT id, category, price, area, project_id, district_id, latitude, longitude,
                FROM real_esates
                WHERE id != %s
                  AND category = %s
                  AND city_id = %s
                  AND price_vnd BETWEEN %s AND %s
                  AND acreage BETWEEN %s AND %s
                LIMIT 100
            """
            cursor.execute(query_candidates, (
                real_estate_id, base_prop['category_id'], base_prop['city'],
                price_min, price_max, area_min, area_max
            ))
            candidates = cursor.fetchall()

            # Bước 3: Tính toán điểm tương đồng chi tiết cho từng ứng viên
            scored_candidates = []
            for item in candidates:
                # 1. Điểm tương đồng giá
                price_diff_ratio = abs(float(item['price']) - float(base_prop['price'])) / float(base_prop['price'])
                price_score = max(0.0, 1.0 - (price_diff_ratio / 0.3))

                # 2. Điểm tương đồng diện tích
                area_diff_ratio = abs(float(item['area']) - float(base_prop['area'])) / float(base_prop['area'])
                area_score = max(0.0, 1.0 - (area_diff_ratio / 0.4))

                # 3. Điểm tương đồng vị trí địa lý dựa vào khoảng cách
                # dist = float(item['distance_km'] or 999.0)
                # if dist <= 1.0:
                #     location_score = 1.0
                # elif dist <= 5.0:
                #     location_score = 1.0 - ((dist - 1.0) / 4.0)
                # else:
                #     location_score = 0.0

                # Cộng điểm ưu tiên nếu cùng Quận/Huyện
                # if item['city'] == base_prop['city']:
                #     location_score = min(1.0, location_score + 0.2)

                # 4. Cộng điểm dự án (Project Boost)
                project_boost = 0.0
                if base_prop['project_id'] and item['project_id'] == base_prop['project_id']:
                    project_boost = 0.3

                # Tính tổng điểm có trọng số: Vị trí (40%), Giá (35%), Diện tích (25%) + Boost dự án
                total_score =   (price_score * 0.35) + (area_score * 0.25) + project_boost
                scored_candidates.append((item['id'], total_score))

            # Sắp xếp các ứng viên giảm dần theo điểm tương đồng
            scored_candidates.sort(key=lambda x: x[1], reverse=True)

            return [item[0] for item in scored_candidates[:limit]]

        except Exception as e:
            logger.error(f"Lỗi khi tính Content-Based recommendations: {str(e)}")
            return []
        finally:
            cursor.close()
