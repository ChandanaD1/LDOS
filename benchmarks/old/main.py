from itertools import permutations, combinations, combinations_with_replacement, product
from math import comb
from collections import Counter

def problem(): 
    print(list(permutations([1,2,3,4],2)))
    print(list(combinations([1,2,3,4],2)))
    print(list(product([1,2,3,4], repeat=2)))
    print(list(combinations_with_replacement([1,2,3,4], 2)))
    print()

    #probablity that two dice sum to 8
    outcomes = list(combinations_with_replacement(range(1, 7), 2))  # all 36 outcomes
    favorable = [o for o in outcomes if sum(o) == 8]  # keep the pairs that sum to 8
    print(favorable)
    print(len(favorable) / len(outcomes))
    print()

    #probability of 2 aces in a 5-card hand
    print((comb(48,3)*comb(4,2)) / comb(52,5))  #4/52 * 3/51 * 48/50 * 47/49 * 46/48
    print()

    #is this 5-card hand a full house (three of one rank, two of another)
    hand = [5,5,9,9,9]
    cnt = Counter(hand)
    print(sorted(list(cnt.values())) == [2,3]) 

#permutations(nums) that returns all permutations of a list of distinct integers
def permutations(nums, k):
    result = []

    def backtrack(current_comb, start):
        if len(current_comb) == k: 
            result.append(current_comb.copy())
            return

        for i in range(start, len(nums)):
            current_comb.append(nums[i])
            backtrack(current_comb, i+1)
            current_comb.pop()


if __name__ == "__main__":
    #problem()
    print(permutations([1,2,3,4]))