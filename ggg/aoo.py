class Solution:
    def rotateGrid(self, grid: List[List[int]], k: int) -> List[List[int]]:
        m, n = len(grid), len(grid[0])
        lyr = min(m, n) // 2
        
        for o in range(lyr):
         
            top, left = o, o
            bottom,right = m - 1 - o , n - 1 - o
            
           
            elements = []
            
          
            for j in range(left ,right):
                elements.append(grid[top][j])
        
            for i in range(top,bottom):

                elements.append(grid[i][right])
       
            for j in range(right,left, -1):
                elements.append(grid[bottom][j])
           
            for i in range(bottom,top, -1):
                elements.append(grid[i][left])
            
            L = len(elements)
            real_k = k % L
            rotate = elements[real_k:]+ elements[:real_k]
            
       
            idx = 0
            for j in range(left,right):




                grid[top][j]  = rotate[idx]
                idx += 1
            for i in range(top,bottom):
                grid[i][right]  = rotate[idx]
                idx += 1
            for j in range(right,left, -1):
                grid[bottom][j] = rotate[idx]
                idx += 1
            for i in range(bottom,top, -1):
                grid[i][left] = rotate[idx]
                idx += 1
                
        return grid