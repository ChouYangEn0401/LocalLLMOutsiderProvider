"""worker：在本機 LLM 伺服器旁執行，主動連到閘道、驗簽、解密、呼叫後端並封裝回覆。

worker 沒有任何監聽中的 socket；它只對外撥出 WebSocket 連線，以及對本機迴圈位址的後端發 HTTP 請求。
"""
