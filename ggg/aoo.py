class Solution:
    def maxPathScore(self, grid: List[List[int]], k: int) -> int:
        m, n = len(grid), len(grid[0])
        
        
        costs = [0, 1, 1]
        scr = [0, 1, 2]
        
       
        dp = [[[-1] * (k + 1) for _ in range(n)] for _ in range(m)]
        
        sv = grid[0][0]
        sc = costs[sv]
        
       
        if sc > k:
            return -1
            
        dp[0][0][sc] = scr[sv]
        
       
        for i in range(m):
            for j in range(n):
                
                if i == 0 and j == 0:
                    continue
                    
                val = grid[i][j]
                new = costs[val]
                news = scr[val]
                
               
                for c in range(new, k + 1):
                    prev_cost = c - new
                    
                    max1 = -1
                    
                   
                    if i > 0 and dp[i-1][j][prev_cost] != -1:
                        max1 = max(max1, dp[i-1][j][prev_cost] + news)
                        
                   
                    if j > 0 and dp[i][j-1][prev_cost] != -1:
                        max1 = max(max1, dp[i][j-1][prev_cost] + news)
                        
                    dp[i][j][c] = max1
                    
        
        ans = max(dp[m-1][n-1])
        return ans