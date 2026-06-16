import asyncio
import sys
from gemini_webapi import GeminiClient

# Đảm bảo in tiếng Việt ra console không bị lỗi Unicode
sys.stdout.reconfigure(encoding='utf-8')

SECURE_1PSID = "g.a000_AjYuI_xzJbWKdaQ-Vh1O4BvZqNjq5NuDjfrrE2U8AKnAjtzZcyQ8Y1S3PQx26KDRPiWjAACgYKAZ8SARYSFQHGX2MiWbE39ZNBHCahqqwmum1h8hoVAUF8yKp70iSvj-LLuwyTioABObAe0076"
SECURE_1PSIDTS = "sidts-CjYByojQUx_NAcNLc5_XGJdNzcO5e54xsvLiMGb81JlyQpsnCzc3dpBERsT-JebpUzxLs9GeyaAQAA"

async def main():
    print("Khởi tạo GeminiClient với cookie người dùng...")
    client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS, verify=False)
    
    try:
        await client.init(timeout=30, auto_close=False, auto_refresh=True)
        print("Khởi tạo thành công!")
        
        print("Đang gửi câu hỏi thử nghiệm...")
        response = await client.generate_content("Chào Gemini, phản hồi ngắn gọn bằng tiếng Việt rằng bạn đang hoạt động bình thường nhé.")
        print("\n--- Phản hồi từ Gemini ---")
        print(response.text)
        
    except Exception as e:
        print(f"\n[LỖI] Đã xảy ra lỗi trong quá trình chạy: {e}")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
